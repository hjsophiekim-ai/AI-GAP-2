"""
adaptive_market_regime.py — ADAPTIVE_MARKET_REGIME: 신규진입(PRIMARY_TREND)과
청산(Dynamic Exit의 7개 market_type)이 서로 다른 장세판단을 쓰던 것을 하나로
통일한 공용 엔진.

이전에는:
  - 신규진입 게이트: PRIMARY_TREND(UP/DOWN/RANGE) — hynix_primary_trend.py
  - 청산 판단: market_type(LOW_VOLATILITY/NORMAL/HIGH_VOLATILITY/TREND_UP/
    TREND_DOWN/PANIC/SHORT_SQUEEZE) — dynamic_exit_engine.py
  - Big Trend Holding AI: 또 다른 자체 regime(STRONG_TREND/NORMAL_TREND/RANGE/
    WHIPSAW/PANIC/REVERSAL_RISK/RECOVERY) — hynix_big_trend_engine.py
서로 다른 기준으로 장세를 각자 판단해, 진입은 "추세 확정"으로 보는데 청산은
"고변동" 또는 그 반대로 보는 식의 충돌이 날 수 있었다.

이 모듈은 000660(SK하이닉스)의 갭/VWAP/5·15·30분 추세/EMA/고저점 구조/ATR/
볼린저 폭/최근 3·5분 수익률/상대거래량/VWAP 교차횟수/스윙 방향전환횟수/방향
이동효율을 입력으로 받아 8개 통일 장세(STRONG_UP/STRONG_DOWN/RANGE/
VOLATILE_RANGE/HIGH_VOLATILITY/PANIC/REVERSAL/DATA_INSUFFICIENT) 중 하나로
분류하고, 장세별 진입비중/익절/손절/트레일링/최대보유시간 프로필을 함께
반환한다. hynix_primary_trend.py의 신규진입 게이트, dynamic_exit_engine.py의
청산 판단, hynix_big_trend_engine.py의 Big Trend Holding이 모두 이 결과를
공유한다(하위 호환을 위해 각 모듈이 자기 자신의 regime 이름으로 매핑해 쓴다).

VOLATILE_RANGE(2026-07-16 추가) — 좁은 박스 안에서 빠르게 위아래로 휩쏘하는
장세(추세도 아니고 조용한 RANGE도 아님)를 별도로 구분한다: 최근 30~60분 VWAP
상·하향 교차가 3회 이상이면서 ATR 기준 변동성 자체는 낮지 않은데(그렇지 않으면
좁고 조용한 RANGE와 구분이 안 됨), 15/30분 추세 불일치·잦은 스윙 반전·낮은
방향 이동효율 중 최소 1개가 함께 확인될 때만 분류한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

# ── 공통 장세(요구사항 1) ────────────────────────────────────────────────────
STRONG_UP = "STRONG_UP"
STRONG_DOWN = "STRONG_DOWN"
RANGE = "RANGE"
VOLATILE_RANGE = "VOLATILE_RANGE"
HIGH_VOLATILITY = "HIGH_VOLATILITY"
PANIC = "PANIC"
REVERSAL = "REVERSAL"
DATA_INSUFFICIENT = "DATA_INSUFFICIENT"

ALL_REGIMES = (STRONG_UP, STRONG_DOWN, RANGE, VOLATILE_RANGE, HIGH_VOLATILITY, PANIC, REVERSAL, DATA_INSUFFICIENT)

_GAP_FLAT_THRESHOLD_PCT = 0.15
_EMA_FLAT_SLOPE_PCT = 0.02
_MIN_BARS_REQUIRED = 20

# PANIC 기본 임계값(요구사항 3) — ATR로 동적 보정한다.
_PANIC_RETURN_3M_BASE_PCT = -1.5
_PANIC_RELATIVE_VOLUME_MIN = 2.0
_PANIC_ATR_REF_PCT = 1.5  # 이 ATR%를 기준으로 임계값을 넓히거나 좁힌다.

# VOLATILE_RANGE 판단 임계값(2026-07-16 요구사항, 최신 개정판 — 최근 30분 기준).
# VWAP 교차 3회 이상 + ATR 기준 변동성이 실제로 있어야(그래야 좁고 조용한
# RANGE와 구분됨) 하고, 나머지 보조 신호(추세 불일치/잦은 스윙반전≥3회/낮은
# 방향이동효율) 중 최소 1개가 더 확인돼야 한다.
_VOLATILE_RANGE_LOOKBACK_MINUTES = 30
_VOLATILE_RANGE_VWAP_CROSS_MIN = 3
_VOLATILE_RANGE_ATR_MIN_PCT = 1.0
_VOLATILE_RANGE_SWING_REVERSAL_MIN = 3
_VOLATILE_RANGE_EFFICIENCY_MAX = 0.35

# STRONG_UP/DOWN 판단 임계값(2026-07-16 요구사항, 큰 추세 수익 극대화판) — VWAP/
# 15·30분추세/고저구조 정렬(이미 구현됨)에 더해, 추세 지속시간·방향이동효율·
# 거래량 또는 ATR 확인을 추가로 요구한다. "2회 연속 확인"은
# update_regime_confirmation()의 2-사이클 확인 절차가 담당한다(여기서는 순간
# 스냅샷만 검사).
_STRONG_TREND_MIN_DURATION_MINUTES = 15
_STRONG_TREND_MIN_EFFICIENCY = 0.5
_STRONG_TREND_MIN_RELATIVE_VOLUME = 1.2
_STRONG_TREND_MIN_ATR_PCT = 0.5

# ── 요구사항 2 — 장세별 리스크 프로필 ────────────────────────────────────────
# position_pct_multiplier: 기존 사이징(EXPLORATORY 30%/CONFIRMED 50% 등) 위에
# 곱해지는 배율. 별도 언급 없는 장세(RANGE/STRONG_TREND류)는 1.0(변경 없음).
RISK_PROFILES: dict[str, dict] = {
    RANGE: {
        "tp1_pct": 1.5, "tp1_ratio": 0.5, "tp2_pct": 2.0, "tp2_ratio": 1.0,
        "sl_pct": 0.8, "uses_trailing": False, "trailing_pct": None,
        "max_hold_minutes": 20, "position_pct_multiplier": 1.0,
    },
    STRONG_UP: {
        # 요구사항(2026-07-16, 큰 추세 수익 극대화판) — +2%에서 20~30%만 부분
        # 익절하고 나머지 70~80%는 ATR trailing(uses_trailing)으로 추세를 끝까지
        # 태운다. 초기 손절 최대 -1.5%. Profit Lock(+3% 이상 시 최소 +1.5% 잠금)은
        # 이 프로필과 무관하게 compute_profit_lock_floor()의 공통 사다리
        # (3%→2.0% 잠금)가 이미 이 요구사항보다 보수적으로 충족한다.
        "tp1_pct": 2.0, "tp1_ratio": 0.25, "tp2_pct": None, "tp2_ratio": None,
        "sl_pct": 1.5, "uses_trailing": True, "trailing_pct": 1.25,
        "max_hold_minutes": None, "position_pct_multiplier": 1.0,
        "core_position_ratio": 0.75,
    },
    STRONG_DOWN: {
        "tp1_pct": 2.0, "tp1_ratio": 0.25, "tp2_pct": None, "tp2_ratio": None,
        "sl_pct": 1.5, "uses_trailing": True, "trailing_pct": 1.25,
        "max_hold_minutes": None, "position_pct_multiplier": 1.0,
        "core_position_ratio": 0.75,
    },
    VOLATILE_RANGE: {
        # 요구사항(2026-07-16, 초단기 실행모드 최종판) — 좁은 박스 안에서 빠르게
        # 휩쏘하는 장에서 뒤늦게 추격매수하지 않고 작게 진입해 빠르게 치고 빠진다.
        # tp1(부분)=+0.8%/50%, tp2(전량)=+1.3%, sl(전량)=-0.6%, 최대 보유 8분.
        # 진입비중은 확인횟수 기반 단계식(entry_stage_pct)으로 관리한다 — 최초
        # confirmation=10%, 2회 확인=20~25%(entry_stage_2_pct). Big Trend
        # Holding과 넓은 레거시 손절폭은 이 장세에서 명시적으로 금지한다
        # (block_big_trend_holding/block_wide_legacy_stop_loss).
        # opposite_signal_reduce_confirmations=1(50% 축소)/
        # opposite_signal_exit_confirmations=2(전량청산) — 반대 강신호 확인횟수별
        # 단계적 대응. chase_block_move_pct=0.7 — 신호 발생가 대비 실제 ETF가
        # (0193T0/0197X0) 가격이 이미 0.7% 이상 움직였으면 추격진입을 CHASE_BLOCK으로
        # 취소한다. no_chase_at_recent_extreme_minutes=3 — 최근 3분 고점/저점에서는
        # 추격진입 자체를 금지한다. pullback_wait_max_seconds=30 — 눌림목 대기는
        # 최대 30초. fast_watcher_interval_seconds=17.5(15~20초) — 이 장세에서
        # Fast Watcher는 더 빠른 주기로 재확인한다. switch_recheck_seconds=20 —
        # 기존 포지션 전량청산·체결확인 후 반대 ETF 즉시 전액매수를 금지하고, 20초
        # 재확인 뒤 10%(switch_reentry_pct) 탐색진입만 허용한다.
        "tp1_pct": 0.8, "tp1_ratio": 0.5, "tp2_pct": 1.3, "tp2_ratio": 1.0,
        "sl_pct": 0.6, "uses_trailing": False, "trailing_pct": None,
        "max_hold_minutes": 8, "position_pct_multiplier": 0.10,
        "position_pct_min": 0.10, "position_pct_max": 0.25,
        "entry_stage_1_pct": 0.10, "entry_stage_2_pct": 0.225,
        "block_big_trend_holding": True, "block_wide_legacy_stop_loss": True,
        "exit_on_opposite_signal_confirmations": 2,
        "opposite_signal_reduce_confirmations": 1, "opposite_signal_reduce_ratio": 0.5,
        "opposite_signal_exit_confirmations": 2,
        "require_box_edge_entry": True, "box_edge_zone_pct": 0.25,
        "consecutive_loss_threshold": 2, "consecutive_loss_cooldown_minutes": 20,
        "chase_block_move_pct": 0.7, "no_chase_at_recent_extreme_minutes": 3,
        "pullback_wait_max_seconds": 30, "fast_watcher_interval_seconds": 17.5,
        "switch_recheck_seconds": 20, "switch_reentry_pct": 0.10,
    },
    HIGH_VOLATILITY: {
        "tp1_pct": 2.5, "tp1_ratio": 0.5, "tp2_pct": 3.5, "tp2_ratio": 1.0,
        "sl_pct": 1.0, "uses_trailing": False, "trailing_pct": None,
        "max_hold_minutes": None, "position_pct_multiplier": 0.5,
    },
    PANIC: {
        "tp1_pct": 1.0, "tp1_ratio": 0.5, "tp2_pct": 2.0, "tp2_ratio": 1.0,
        "sl_pct": 0.7, "uses_trailing": False, "trailing_pct": None,
        "max_hold_minutes": 10, "position_pct_multiplier": 0.15,
        "position_pct_min": 0.10, "position_pct_max": 0.20,
    },
    REVERSAL: {
        # 요구사항2 — REVERSAL은 "기존 포지션 우선 청산 후 반대 ETF 탐색진입"이
        # 목적이므로 고정 TP/SL 폭보다 "즉시 재평가/청산 우선"이 핵심이다.
        # 신규 탐색진입은 EXPLORATORY 취급(작은 비중)한다.
        "tp1_pct": 1.0, "tp1_ratio": 1.0, "tp2_pct": None, "tp2_ratio": None,
        "sl_pct": 0.8, "uses_trailing": False, "trailing_pct": None,
        "max_hold_minutes": None, "position_pct_multiplier": 0.5,
        "force_exit_existing_position": True,
    },
    DATA_INSUFFICIENT: {
        # 요구사항2 — 신규주문 금지. 기존 보유 포지션은 가장 보수적인 기본값으로
        # 방어한다(신뢰할 수 있는 신호가 없으므로 판단을 확대하지 않는다).
        "tp1_pct": 3.0, "tp1_ratio": 1.0, "tp2_pct": None, "tp2_ratio": None,
        "sl_pct": 1.5, "uses_trailing": False, "trailing_pct": None,
        "max_hold_minutes": 30, "position_pct_multiplier": 0.0,
        "block_new_entries": True,
    },
}


def get_risk_profile(regime: str) -> dict:
    """장세 이름으로 리스크 프로필을 조회한다. 모르는 값이면 RANGE(가장 보수적인
    기본값)로 폴백한다."""
    return dict(RISK_PROFILES.get(regime, RISK_PROFILES[RANGE]))


# ── 지표 계산 헬퍼(hynix_primary_trend.py/dynamic_exit_engine.py와 동일 관례) ──

def _ema_slope_pct(closes: pd.Series, span: int) -> Optional[float]:
    if closes is None or len(closes) < 2:
        return None
    ema = closes.ewm(span=min(span, len(closes)), adjust=False).mean()
    if len(ema) < 2 or not ema.iloc[-2]:
        return None
    return round((float(ema.iloc[-1]) / float(ema.iloc[-2]) - 1.0) * 100.0, 4)


def _slope_to_direction(slope_pct: Optional[float]) -> str:
    if slope_pct is None:
        return "FLAT"
    if slope_pct >= _EMA_FLAT_SLOPE_PCT:
        return "UP"
    if slope_pct <= -_EMA_FLAT_SLOPE_PCT:
        return "DOWN"
    return "FLAT"


def _swing_structure(df, lookback: int = 4) -> dict:
    if df is None or len(df) < lookback:
        return {"higher_high": False, "higher_low": False, "lower_high": False, "lower_low": False}
    work = df.sort_values("datetime").tail(lookback)
    highs, lows = work["high"].tolist(), work["low"].tolist()
    return {
        "higher_high": all(highs[i] >= highs[i - 1] for i in range(1, len(highs))),
        "higher_low": all(lows[i] >= lows[i - 1] for i in range(1, len(lows))),
        "lower_high": all(highs[i] <= highs[i - 1] for i in range(1, len(highs))),
        "lower_low": all(lows[i] <= lows[i - 1] for i in range(1, len(lows))),
    }


def _daily_vwap(df) -> Optional[float]:
    if df is None or df.empty or "volume" not in df.columns:
        return None
    vol = df["volume"].fillna(0)
    if vol.sum() <= 0:
        return round(float(df["close"].mean()), 4)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    return round(float((typical * vol).sum() / vol.sum()), 4)


def _relative_volume(df, recent: int = 5, baseline: int = 20) -> Optional[float]:
    if df is None or len(df) < recent or "volume" not in df.columns:
        return None
    work = df.sort_values("datetime")
    recent_vol = work["volume"].tail(recent).mean()
    base_vol = work["volume"].tail(min(baseline, len(work))).mean()
    if not base_vol:
        return None
    return round(float(recent_vol / base_vol), 4)


def _atr_pct(df, period: int = 14) -> Optional[float]:
    if df is None or len(df) < period + 1:
        return None
    work = df.sort_values("datetime")
    closes, highs, lows = work["close"], work["high"], work["low"]
    prev_close = closes.shift(1)
    tr = pd.concat([highs - lows, (highs - prev_close).abs(), (lows - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    current = float(closes.iloc[-1])
    return round(float(atr) / current * 100, 4) if current > 0 and pd.notna(atr) else None


def _bollinger_width_pct(closes: pd.Series, period: int = 20, num_std: float = 2.0) -> Optional[float]:
    if closes is None or len(closes) < period:
        return None
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    if pd.isna(mid.iloc[-1]) or not mid.iloc[-1]:
        return None
    upper = mid.iloc[-1] + num_std * std.iloc[-1]
    lower = mid.iloc[-1] - num_std * std.iloc[-1]
    return round(float((upper - lower) / mid.iloc[-1] * 100), 4)


def _return_pct_over_minutes(df_1min, minutes: int) -> Optional[float]:
    if df_1min is None or len(df_1min) < 2:
        return None
    work = df_1min.sort_values("datetime").tail(minutes + 1)
    if len(work) < 2:
        return None
    first, last = float(work.iloc[0]["close"]), float(work.iloc[-1]["close"])
    return round((last / first - 1.0) * 100, 4) if first > 0 else None


def _recent_window(df, lookback_minutes: int):
    if df is None or df.empty:
        return None
    work = df.sort_values("datetime")
    cutoff = work["datetime"].iloc[-1] - pd.Timedelta(minutes=lookback_minutes)
    return work[work["datetime"] >= cutoff]


def _count_vwap_crosses(df_1min, lookback_minutes: int) -> Optional[int]:
    """최근 lookback_minutes 동안 종가가 누적VWAP선을 몇 번 오르내렸는지 센다
    (VOLATILE_RANGE 판단 요구사항 — "최근 30~60분 VWAP 상·하향 교차 3회 이상")."""
    recent = _recent_window(df_1min, lookback_minutes)
    if recent is None or len(recent) < 5 or "volume" not in recent.columns:
        return None
    vol = recent["volume"].fillna(0)
    cum_vol = vol.cumsum()
    if float(cum_vol.iloc[-1]) <= 0:
        return None
    typical = (recent["high"] + recent["low"] + recent["close"]) / 3.0
    vwap = (typical * vol).cumsum() / cum_vol.replace(0, pd.NA)
    diff = recent["close"] - vwap
    sign = diff.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    sign = sign[sign != 0]
    if len(sign) < 2:
        return 0
    return int((sign.diff().fillna(0) != 0).sum())


def _count_swing_reversals(df_5min, lookback_minutes: int) -> Optional[int]:
    """최근 lookback_minutes 동안 5분봉 종가의 상승/하락 방향이 몇 번 뒤집혔는지
    센다(VOLATILE_RANGE 판단 요구사항 — "최근 스윙 방향전환이 빈번")."""
    recent = _recent_window(df_5min, lookback_minutes)
    if recent is None or len(recent) < 4:
        return None
    closes = recent["close"].tolist()
    directions = [1 if closes[i] > closes[i - 1] else (-1 if closes[i] < closes[i - 1] else 0) for i in range(1, len(closes))]
    directions = [d for d in directions if d != 0]
    if len(directions) < 2:
        return 0
    return sum(1 for i in range(1, len(directions)) if directions[i] != directions[i - 1])


def _directional_efficiency_ratio(df_1min, lookback_minutes: int) -> Optional[float]:
    """Kaufman Efficiency Ratio — 순변화량 / 총이동거리. 낮을수록(0에 가까울수록)
    같은 자리를 오가는 휩쏘/횡보 구간이고, 1에 가까우면 한 방향으로 곧게 이동한
    구간이다(VOLATILE_RANGE 판단 요구사항 — "순방향 이동효율은 낮음")."""
    recent = _recent_window(df_1min, lookback_minutes)
    if recent is None or len(recent["close"]) < 5:
        return None
    closes = recent["close"]
    net_change = abs(float(closes.iloc[-1]) - float(closes.iloc[0]))
    path_length = float(closes.diff().abs().sum())
    if path_length <= 0:
        return None
    return round(net_change / path_length, 4)


def _box_bounds(df_1min, lookback_minutes: int) -> tuple[Optional[float], Optional[float]]:
    """UI에 표시할 "박스 상단/하단" — 최근 lookback_minutes 동안의 고가/저가."""
    recent = _recent_window(df_1min, lookback_minutes)
    if recent is None or recent.empty:
        return None, None
    return round(float(recent["high"].max()), 2), round(float(recent["low"].min()), 2)


def _trend_duration_minutes(df_1min, direction: str) -> Optional[int]:
    """지금 이 순간부터 거슬러 올라가며, 종가가 누적VWAP 기준으로 계속
    direction("UP"=위/"DOWN"=아래) 쪽에 머문 연속 분(分)을 센다(STRONG_UP/DOWN
    요구사항 — "추세 지속시간 15분 이상")."""
    if df_1min is None or df_1min.empty or "volume" not in df_1min.columns:
        return None
    work = df_1min.sort_values("datetime")
    vol = work["volume"].fillna(0)
    cum_vol = vol.cumsum()
    if float(cum_vol.iloc[-1]) <= 0:
        return None
    typical = (work["high"] + work["low"] + work["close"]) / 3.0
    vwap = (typical * vol).cumsum() / cum_vol.replace(0, pd.NA)
    diff = (work["close"] - vwap).tolist()
    duration = 0
    for val in reversed(diff):
        if pd.isna(val):
            break
        if direction == "UP" and val > 0:
            duration += 1
        elif direction == "DOWN" and val < 0:
            duration += 1
        else:
            break
    return duration


def classify_raw_regime(
    df_1min: Optional[pd.DataFrame], df_daily: Optional[pd.DataFrame] = None,
    *, prev_close: Optional[float] = None, now: Optional[datetime] = None,
) -> dict:
    """현재 스냅샷만으로(과거 confirmed 장세 이력 없이) 순간 장세를 분류한다.

    REVERSAL은 여기서 나오지 않는다 — REVERSAL은 "이전에 확정된 장세와 지금
    관측되는 방향이 반대"라는 전환(transition) 개념이라 이전 상태가 필요하며,
    update_regime_confirmation()이 그 비교를 담당한다.
    """
    from app.utils.time_utils import kst_now

    now = now or kst_now()
    result = {
        "regime": DATA_INSUFFICIENT, "confidence": 0.0, "reasons": ["1분봉 데이터 없음(df_1min=None)"],
        "gap_direction": None, "gap_pct": None, "above_vwap": None, "vwap": None,
        "trend_5m": "FLAT", "trend_15m": "FLAT", "trend_30m": "FLAT",
        "ema20_slope_pct": None, "swing": {}, "atr_pct": None, "bollinger_width_pct": None,
        "return_3m_pct": None, "return_5m_pct": None, "relative_volume": None,
        "up_votes": 0, "down_votes": 0, "computed_at": now.isoformat(timespec="seconds"),
        # VOLATILE_RANGE 진단(UI 표시용, 항상 채워짐 — 계산 불가 시 None) —
        # 박스 상단/하단, VWAP 교차횟수, 방향 이동효율.
        "vwap_cross_count": None, "swing_reversal_count": None, "efficiency_ratio": None,
        "box_high": None, "box_low": None,
        # STRONG_UP/DOWN 진단(UI 표시용) — 추세 지속시간(분).
        "trend_duration_minutes": None,
    }
    # 요구사항(2026-07-16) — 데이터가 부족하면 "비활성화"가 아니라 DATA_INSUFFICIENT로
    # 표시하되, 정확히 무엇이 부족한지(1분봉 자체가 없는지/행 수가 모자란지/유효한
    # 종가가 없는지) UI가 그대로 보여줄 수 있게 구체적인 사유를 남긴다.
    if df_1min is None:
        result["reasons"] = ["1분봉 데이터 없음(df_1min=None) — 시세 수집 실패"]
        return result
    if getattr(df_1min, "empty", True):
        result["reasons"] = ["1분봉 데이터가 빈 데이터프레임(0행)"]
        return result
    if len(df_1min) < _MIN_BARS_REQUIRED:
        result["reasons"] = [f"1분봉 {len(df_1min)}개만 확보(최소 {_MIN_BARS_REQUIRED}개 필요)"]
        return result

    work = df_1min.sort_values("datetime").copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["close"])
    if work.empty:
        result["reasons"] = ["1분봉에 유효한 종가(close)가 하나도 없음(전부 결측/변환 실패)"]
        return result

    last_close = float(work["close"].iloc[-1])

    if prev_close and prev_close > 0:
        open_price = float(work["open"].iloc[0]) if "open" in work.columns else last_close
        gap_pct = (open_price / prev_close - 1.0) * 100.0
        result["gap_pct"] = round(gap_pct, 4)
        if gap_pct >= _GAP_FLAT_THRESHOLD_PCT:
            result["gap_direction"] = "UP"
        elif gap_pct <= -_GAP_FLAT_THRESHOLD_PCT:
            result["gap_direction"] = "DOWN"
        else:
            result["gap_direction"] = "FLAT"

    vwap = _daily_vwap(work)
    result["vwap"] = vwap
    result["above_vwap"] = (last_close >= vwap) if vwap is not None else None

    try:
        from app.data_sources.auto_market_collector import _resample_minutes

        df_5 = _resample_minutes(work, 5)
        df_15 = _resample_minutes(work, 15)
        df_30 = _resample_minutes(work, 30)
    except Exception:
        df_5 = df_15 = df_30 = None

    slope_5 = _ema_slope_pct(df_5["close"], span=6) if df_5 is not None and len(df_5) >= 2 else None
    slope_15 = _ema_slope_pct(df_15["close"], span=6) if df_15 is not None and len(df_15) >= 2 else None
    slope_30 = _ema_slope_pct(df_30["close"], span=6) if df_30 is not None and len(df_30) >= 2 else None
    result["trend_5m"] = _slope_to_direction(slope_5)
    result["trend_15m"] = _slope_to_direction(slope_15)
    result["trend_30m"] = _slope_to_direction(slope_30)

    if len(work["close"]) >= 20:
        ema20 = work["close"].ewm(span=20, adjust=False).mean()
        if len(ema20) >= 2 and float(ema20.iloc[-2]):
            result["ema20_slope_pct"] = round((float(ema20.iloc[-1]) / float(ema20.iloc[-2]) - 1.0) * 100.0, 4)

    swing_15 = _swing_structure(df_15) if df_15 is not None else {}
    result["swing"] = swing_15
    result["atr_pct"] = _atr_pct(work)
    result["bollinger_width_pct"] = _bollinger_width_pct(work["close"])
    result["return_3m_pct"] = _return_pct_over_minutes(work, 3)
    result["return_5m_pct"] = _return_pct_over_minutes(work, 5)
    result["relative_volume"] = _relative_volume(work)
    result["vwap_cross_count"] = _count_vwap_crosses(work, _VOLATILE_RANGE_LOOKBACK_MINUTES)
    result["swing_reversal_count"] = _count_swing_reversals(df_5, _VOLATILE_RANGE_LOOKBACK_MINUTES)
    result["efficiency_ratio"] = _directional_efficiency_ratio(work, _VOLATILE_RANGE_LOOKBACK_MINUTES)
    result["box_high"], result["box_low"] = _box_bounds(work, _VOLATILE_RANGE_LOOKBACK_MINUTES)

    # ── PANIC(요구사항 3) — ATR로 임계값 동적 보정 ──────────────────────────
    ret3m, rel_vol, atr_pct = result["return_3m_pct"], result["relative_volume"], result["atr_pct"]
    if ret3m is not None and rel_vol is not None:
        atr_ref = atr_pct if atr_pct and atr_pct > 0 else _PANIC_ATR_REF_PCT
        # ATR이 기준보다 크면(원래도 변동성이 큰 종목/시점) 더 큰 하락폭을 요구하고,
        # ATR이 작으면(평소 조용한데 갑자기 급락) 임계값을 더 민감하게(덜 음수로) 만든다.
        dynamic_threshold = _PANIC_RETURN_3M_BASE_PCT * (atr_ref / _PANIC_ATR_REF_PCT)
        if ret3m <= dynamic_threshold and rel_vol >= _PANIC_RELATIVE_VOLUME_MIN:
            result["regime"] = PANIC
            result["confidence"] = min(100.0, 60.0 + (rel_vol - _PANIC_RELATIVE_VOLUME_MIN) * 10.0)
            result["reasons"] = [
                f"3분 수익률 {ret3m:.2f}% ≤ 동적임계값 {dynamic_threshold:.2f}%(ATR {atr_ref:.2f}% 기준)",
                f"상대거래량 {rel_vol:.2f}배 ≥ {_PANIC_RELATIVE_VOLUME_MIN}배",
            ]
            return result

    # ── STRONG_UP/STRONG_DOWN — gap+VWAP+5/15/30분 추세+스윙구조 투표 ────────
    up_votes = down_votes = 0
    reasons: list[str] = []
    if result["gap_direction"] == "UP":
        up_votes += 1; reasons.append("gap up")
    elif result["gap_direction"] == "DOWN":
        down_votes += 1; reasons.append("gap down")
    if result["above_vwap"] is True:
        up_votes += 1; reasons.append("above VWAP")
    elif result["above_vwap"] is False:
        down_votes += 1; reasons.append("below VWAP")
    for label, tf in (("5m", result["trend_5m"]), ("15m", result["trend_15m"]), ("30m", result["trend_30m"])):
        if tf == "UP":
            up_votes += 1; reasons.append(f"{label} trend UP")
        elif tf == "DOWN":
            down_votes += 1; reasons.append(f"{label} trend DOWN")
    if swing_15.get("higher_high") and swing_15.get("higher_low"):
        up_votes += 1; reasons.append("higher high/low structure")
    elif swing_15.get("lower_high") and swing_15.get("lower_low"):
        down_votes += 1; reasons.append("lower high/low structure")
    result["up_votes"], result["down_votes"] = up_votes, down_votes

    # 요구사항(2026-07-16, 큰 추세 수익 극대화판) — STRONG_UP/DOWN은 15분·30분
    # 추세, VWAP, 고저점 구조가 "전부" 같은 방향으로 일치해야 하고(엄격한 AND
    # 조건), 여기에 추세 지속시간(≥15분)·방향이동효율(높음)·거래량 또는 ATR
    # 확인까지 전부 충족해야 후보가 된다. "2회 확인"은 여기서가 아니라
    # update_regime_confirmation()의 2연속 사이클 확인 절차가 담당한다 — 이
    # 함수는 순간 스냅샷만 본다.
    trend_duration_up = _trend_duration_minutes(work, "UP")
    trend_duration_down = _trend_duration_minutes(work, "DOWN")
    result["trend_duration_minutes"] = trend_duration_up if (trend_duration_up or 0) >= (trend_duration_down or 0) else trend_duration_down
    efficiency_for_strong = result["efficiency_ratio"]
    volume_or_atr_confirmed = (
        (result["relative_volume"] is not None and result["relative_volume"] >= _STRONG_TREND_MIN_RELATIVE_VOLUME)
        or (atr_pct is not None and atr_pct >= _STRONG_TREND_MIN_ATR_PCT)
    )
    high_efficiency = efficiency_for_strong is not None and efficiency_for_strong >= _STRONG_TREND_MIN_EFFICIENCY

    strong_up_aligned = (
        result["trend_15m"] == "UP" and result["trend_30m"] == "UP"
        and result["above_vwap"] is True
        and bool(swing_15.get("higher_high")) and bool(swing_15.get("higher_low"))
        and (trend_duration_up or 0) >= _STRONG_TREND_MIN_DURATION_MINUTES
        and high_efficiency and volume_or_atr_confirmed
    )
    strong_down_aligned = (
        result["trend_15m"] == "DOWN" and result["trend_30m"] == "DOWN"
        and result["above_vwap"] is False
        and bool(swing_15.get("lower_high")) and bool(swing_15.get("lower_low"))
        and (trend_duration_down or 0) >= _STRONG_TREND_MIN_DURATION_MINUTES
        and high_efficiency and volume_or_atr_confirmed
    )
    if strong_up_aligned:
        result["regime"] = STRONG_UP
        result["confidence"] = round(min(100.0, 60.0 + up_votes * 6.0), 2)
        result["reasons"] = reasons + [
            f"추세 지속 {trend_duration_up}분", f"방향이동효율 {efficiency_for_strong}",
            f"상대거래량 {result['relative_volume']}/ATR {atr_pct}%로 확인",
        ]
        return result
    if strong_down_aligned:
        result["regime"] = STRONG_DOWN
        result["confidence"] = round(min(100.0, 60.0 + down_votes * 6.0), 2)
        result["reasons"] = reasons + [
            f"추세 지속 {trend_duration_down}분", f"방향이동효율 {efficiency_for_strong}",
            f"상대거래량 {result['relative_volume']}/ATR {atr_pct}%로 확인",
        ]
        return result

    # ── VOLATILE_RANGE(2026-07-16 요구사항) — 좁은 박스 안에서 빠르게 휩쏘 ────
    # 하드 게이트: VWAP 교차 3회 이상 + ATR 기준 변동성이 실제로 있음(그래야 좁고
    # 조용한 RANGE와 구분됨). 이 둘을 만족하면 보조신호(추세 불일치/잦은
    # 스윙반전/낮은 방향이동효율) 중 최소 1개가 더 확인될 때 최종 분류한다.
    vwap_cross_count = result["vwap_cross_count"]
    swing_reversal_count = result["swing_reversal_count"]
    efficiency_ratio = result["efficiency_ratio"]
    volatile_gate = (
        vwap_cross_count is not None and vwap_cross_count >= _VOLATILE_RANGE_VWAP_CROSS_MIN
        and atr_pct is not None and atr_pct >= _VOLATILE_RANGE_ATR_MIN_PCT
    )
    if volatile_gate:
        trend_disagrees = (
            result["trend_15m"] != result["trend_30m"]
            or result["trend_15m"] == "FLAT" or result["trend_30m"] == "FLAT"
        )
        frequent_swing_reversal = swing_reversal_count is not None and swing_reversal_count >= _VOLATILE_RANGE_SWING_REVERSAL_MIN
        low_efficiency = efficiency_ratio is not None and efficiency_ratio <= _VOLATILE_RANGE_EFFICIENCY_MAX
        volatile_reasons = [f"VWAP 교차 {vwap_cross_count}회(최근 {_VOLATILE_RANGE_LOOKBACK_MINUTES}분) — ATR {atr_pct:.2f}%로 변동성 존재"]
        if trend_disagrees:
            volatile_reasons.append(f"15분/30분 추세 불일치 또는 FLAT({result['trend_15m']}/{result['trend_30m']})")
        if frequent_swing_reversal:
            volatile_reasons.append(f"스윙 방향전환 {swing_reversal_count}회")
        if low_efficiency:
            volatile_reasons.append(f"방향 이동효율 {efficiency_ratio} ≤ {_VOLATILE_RANGE_EFFICIENCY_MAX}(휩쏘)")
        if trend_disagrees or frequent_swing_reversal or low_efficiency:
            result["regime"] = VOLATILE_RANGE
            result["confidence"] = round(min(100.0, 55.0 + vwap_cross_count * 5.0), 2)
            result["reasons"] = volatile_reasons
            return result

    # ── HIGH_VOLATILITY — ATR/볼린저폭/5분 수익률 크기 기준 ──────────────────
    vol_signals = []
    if atr_pct is not None:
        vol_signals.append(atr_pct >= 2.2)
    bb = result["bollinger_width_pct"]
    if bb is not None:
        vol_signals.append(bb >= 5.0)
    ret5 = result["return_5m_pct"]
    if ret5 is not None:
        vol_signals.append(abs(ret5) >= 1.2)
    if vol_signals and sum(1 for v in vol_signals if v) >= max(1, len(vol_signals) - 0):
        # 관측된 변동성 신호 전부가 높은 변동성을 가리키면 HIGH_VOLATILITY.
        if all(vol_signals):
            result["regime"] = HIGH_VOLATILITY
            result["confidence"] = 65.0
            result["reasons"] = [f"atr={atr_pct}, bollinger_width={bb}, return_5m={ret5}"]
            return result

    # ── RANGE(기본값) ────────────────────────────────────────────────────────
    result["regime"] = RANGE
    result["confidence"] = round(max(30.0, 50.0 - abs(up_votes - down_votes) * 5.0), 2)
    result["reasons"] = reasons or ["no clear directional or volatility signal"]
    return result


# ── 2회 연속 확인(요구사항4) ──────────────────────────────────────────────────

_CONFIRMATIONS_REQUIRED = 2


def default_regime_confirmation_state() -> dict:
    return {
        "confirmed_regime": DATA_INSUFFICIENT, "candidate_regime": None, "candidate_count": 0,
        "last_confirmed_at": None, "previous_regime": None, "transitioned_at": None,
    }


def update_regime_confirmation(
    state: Optional[dict], raw_regime: str, now: datetime, *, hard_override: Optional[str] = None,
) -> dict:
    """요구사항4 — 장세가 1회 바뀌었다고 즉시 전환하지 않고 2회 연속 확인한다.

    hard_override가 주어지면(하드손절/15:15 강제청산/반대추세 확정) 확인 절차 없이
    즉시 그 장세로 전환한다. raw_regime이 현재 confirmed_regime과 정반대(STRONG_UP
    ↔ STRONG_DOWN)이면, 확인 대기 중에는 REVERSAL로 표시해 "전환 검토 중"임을
    UI/로직에 알린다 — 확인이 끝나면(2회) 실제 새 regime(STRONG_UP/STRONG_DOWN)으로
    확정된다.
    """
    state = dict(state) if state else default_regime_confirmation_state()

    if hard_override:
        if state["confirmed_regime"] != hard_override:
            state["previous_regime"] = state["confirmed_regime"]
            state["transitioned_at"] = now.isoformat(timespec="seconds")
        state["confirmed_regime"] = hard_override
        state["candidate_regime"] = None
        state["candidate_count"] = 0
        state["last_confirmed_at"] = now.isoformat(timespec="seconds")
        return state

    if raw_regime == state["confirmed_regime"]:
        state["candidate_regime"] = None
        state["candidate_count"] = 0
        state["last_confirmed_at"] = now.isoformat(timespec="seconds")
        return state

    if state.get("candidate_regime") == raw_regime:
        state["candidate_count"] = int(state.get("candidate_count", 0)) + 1
    else:
        state["candidate_regime"] = raw_regime
        state["candidate_count"] = 1

    if state["candidate_count"] >= _CONFIRMATIONS_REQUIRED:
        state["previous_regime"] = state["confirmed_regime"]
        state["confirmed_regime"] = raw_regime
        state["candidate_regime"] = None
        state["candidate_count"] = 0
        state["transitioned_at"] = now.isoformat(timespec="seconds")
        state["last_confirmed_at"] = now.isoformat(timespec="seconds")

    return state


def is_opposite_trend(regime_a: str, regime_b: str) -> bool:
    return {regime_a, regime_b} == {STRONG_UP, STRONG_DOWN}


def compute_and_confirm_regime(
    df_1min: Optional[pd.DataFrame], df_daily: Optional[pd.DataFrame] = None,
    *, confirmation_state: Optional[dict] = None, prev_close: Optional[float] = None,
    now: Optional[datetime] = None, hard_override: Optional[str] = None,
) -> dict:
    """신규진입/스위칭/손절/익절/보유시간 판단이 전부 공유하는 단일 진입점
    (요구사항 — "공통 regime을 하나만 계산한다"). classify_raw_regime()의 순간
    스냅샷을 update_regime_confirmation()의 2연속 확인 절차에 넣어, 그 결과로
    확정된 장세(confirmed_regime)와 리스크 프로필을 함께 반환한다.

    Returns: {raw_regime, confirmed_regime, displayed_regime, confidence, reasons,
              profile(get_risk_profile(confirmed_regime) 결과), previous_regime,
              transitioned_at, confirmation_state(다음 호출에 그대로 넘길 갱신된 상태),
              snapshot(classify_raw_regime()의 전체 원본 결과 — box_high/box_low/
              vwap_cross_count 등 UI 진단 필드 포함)}
    """
    raw = classify_raw_regime(df_1min, df_daily, prev_close=prev_close, now=now)
    updated_state = update_regime_confirmation(
        confirmation_state, raw["regime"], now or datetime.now(), hard_override=hard_override,
    )
    confirmed = updated_state["confirmed_regime"]
    shown = displayed_regime(updated_state)
    return {
        "raw_regime": raw["regime"], "confirmed_regime": confirmed, "displayed_regime": shown,
        "confidence": raw.get("confidence"), "reasons": raw.get("reasons"),
        "profile": get_risk_profile(confirmed),
        "previous_regime": updated_state.get("previous_regime"),
        "transitioned_at": updated_state.get("transitioned_at"),
        "confirmation_state": updated_state, "snapshot": raw,
    }


def adaptive_regime_to_primary_trend_result(adaptive_regime_result: Optional[dict]) -> dict:
    """요구사항(2026-07-16, 남은 통합 작업1) — hynix_primary_trend.py의 신규진입
    게이트(evaluate_pullback_gate/new_inverse_entry_blocked/new_hynix_entry_blocked
    등)가 계속 기대하는 PRIMARY_TREND(UP/DOWN/RANGE) 모양의 dict를, compute_and_
    confirm_regime()이 이미 계산한 snapshot에서만 파생시켜 반환한다 — 별도로
    compute_primary_trend()를 다시 호출해 재분류하지 않는다.

    STRONG_UP→"UP", STRONG_DOWN→"DOWN", 그 외(RANGE/VOLATILE_RANGE/
    HIGH_VOLATILITY/PANIC/REVERSAL/DATA_INSUFFICIENT)는 전부 "RANGE"로 매핑한다
    (PRIMARY_TREND은 3단계뿐이므로, adaptive_market_regime의 세분화된 나머지
    장세는 "확정된 강한 추세가 아니다"라는 의미에서 전부 RANGE에 해당한다).
    """
    from app.trading.hynix_primary_trend import PRIMARY_TREND_UP, PRIMARY_TREND_DOWN, PRIMARY_TREND_RANGE

    adaptive_regime_result = adaptive_regime_result or {}
    snapshot = adaptive_regime_result.get("snapshot") or {}
    confirmed = adaptive_regime_result.get("confirmed_regime")
    if confirmed == STRONG_UP:
        primary_trend = PRIMARY_TREND_UP
    elif confirmed == STRONG_DOWN:
        primary_trend = PRIMARY_TREND_DOWN
    else:
        primary_trend = PRIMARY_TREND_RANGE
    return {
        "primary_trend": primary_trend,
        "gap_direction": snapshot.get("gap_direction"), "gap_pct": snapshot.get("gap_pct"),
        "above_vwap": snapshot.get("above_vwap"), "vwap": snapshot.get("vwap"),
        "trend_15m": snapshot.get("trend_15m", "FLAT"), "trend_30m": snapshot.get("trend_30m", "FLAT"),
        "ema20_slope_pct": snapshot.get("ema20_slope_pct"), "swing_15m": snapshot.get("swing") or {},
        "relative_volume": snapshot.get("relative_volume"), "last_price": None,
        "reasons": adaptive_regime_result.get("reasons") or [],
        "up_votes": snapshot.get("up_votes", 0), "down_votes": snapshot.get("down_votes", 0),
        "computed_at": snapshot.get("computed_at"),
    }


def displayed_regime(state: dict) -> str:
    """UI/게이트가 실제로 참고해야 할 "지금 이 순간의" 장세.

    확인 대기 중(candidate_count==1)에 그 후보가 confirmed_regime의 정반대
    방향이면 REVERSAL을 보여준다(요구사항4 "반대 추세 확정은 즉시 우선" —
    확정되기 전까지는 REVERSAL이라는 별도 상태로 노출해 신중하게 취급한다).
    """
    confirmed = state.get("confirmed_regime", DATA_INSUFFICIENT)
    candidate = state.get("candidate_regime")
    if candidate and is_opposite_trend(confirmed, candidate) and state.get("candidate_count", 0) >= 1:
        return REVERSAL
    return confirmed


# ── VOLATILE_RANGE 초단기 실행 보호(2026-07-16) ──────────────────────────────
# 요구사항6 — 신호 자체는 000660 기준으로 계산되지만, 추격진입 판단과 TP/SL은
# 실제 거래 종목인 0193T0/0197X0 가격 기준이어야 한다. 아래 두 함수는 호출자가
# "신호가 처음 뜬 시점의 ETF 실제가"와 "지금 이 순간의 ETF 실제가"를 각각 넘겨
# 판단한다 — 이 모듈은 000660 가격을 전혀 참조하지 않는다.

def is_chase_blocked(signal_reference_price: Optional[float], current_price: Optional[float], regime: str) -> dict:
    """신호 발생 시점의 ETF 실제가(signal_reference_price) 대비 지금 가격이 이미
    chase_block_move_pct% 이상 움직였으면 추격진입을 CHASE_BLOCK으로 취소한다
    (VOLATILE_RANGE 요구사항 — "신호 발생 후 ETF가 이미 0.7% 이상 움직였으면
    CHASE_BLOCK으로 진입 취소"). 프로필에 해당 값이 없는 장세는 항상 통과."""
    profile = get_risk_profile(regime)
    threshold = profile.get("chase_block_move_pct")
    result = {"blocked": False, "moved_pct": None, "threshold_pct": threshold}
    if threshold is None or not signal_reference_price or not current_price:
        return result
    moved_pct = round(abs(float(current_price) / float(signal_reference_price) - 1.0) * 100.0, 4)
    result["moved_pct"] = moved_pct
    result["blocked"] = moved_pct >= threshold
    return result


def is_entry_at_recent_extreme(current_price: Optional[float], df_1min, direction: str, regime: str) -> bool:
    """최근 N분 고점/저점 부근에서의 추격진입을 금지한다(VOLATILE_RANGE 요구사항
    — "최근 3분 고점/저점에서 추격진입 금지"). direction="BUY"는 최근 고점 근접
    매수(상단 추격) 금지, direction="SELL"은 최근 저점 근접 매수(인버스가 하락에
    베팅하므로 하단 부근 추격매수) 금지를 뜻한다."""
    profile = get_risk_profile(regime)
    minutes = profile.get("no_chase_at_recent_extreme_minutes")
    if minutes is None or current_price is None or df_1min is None or getattr(df_1min, "empty", True):
        return False
    recent = _recent_window(df_1min, minutes)
    if recent is None or recent.empty:
        return False
    try:
        recent_high = float(recent["high"].max())
        recent_low = float(recent["low"].min())
    except Exception:
        return False
    if direction == "BUY":
        return current_price >= recent_high * 0.999
    if direction == "SELL":
        return current_price <= recent_low * 1.001
    return False


def opposite_signal_response(opposite_signal_streak: int, regime: str) -> Optional[dict]:
    """반대 강신호 확인횟수에 따른 단계적 대응(VOLATILE_RANGE 요구사항 — "반대
    강신호 1회면 50% 축소, 2회면 전량청산"). 해당 없으면 None."""
    profile = get_risk_profile(regime)
    exit_at = profile.get("opposite_signal_exit_confirmations")
    reduce_at = profile.get("opposite_signal_reduce_confirmations")
    if exit_at is not None and opposite_signal_streak >= exit_at:
        return {"action": "SELL_ALL", "ratio": 1.0, "reason": f"반대 강신호 {opposite_signal_streak}회 확인 — 전량청산"}
    if reduce_at is not None and opposite_signal_streak >= reduce_at:
        ratio = profile.get("opposite_signal_reduce_ratio", 0.5)
        return {"action": "SELL_PARTIAL", "ratio": ratio, "reason": f"반대 강신호 {opposite_signal_streak}회 확인 — {ratio*100:.0f}% 축소"}
    return None
