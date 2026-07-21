"""macd_williams_episode.py — 3분봉 MACD+Williams를 direction_episode 확인기로만 사용.

설계 원칙 (2026-07-22):
  - 단독으로 broker 주문을 내지 않는다.
  - MACD histogram 방향과 Williams %R이 같은 방향이면 episode 확인.
  - 반대 방향이면 enhanced 누적점수가 해당 episode 방향을 덮어쓰지 못하게 한다.
  - 미완성 3분봉은 사용 금지(봉 종료 시각 이후에만 신호).
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
WILLIAMS_PERIOD = 14
WILLIAMS_OVERSOLD = -80.0
WILLIAMS_OVERBOUGHT = -20.0
WILLIAMS_MID = -50.0


def resample_completed_3m(df_1m: pd.DataFrame, now=None) -> pd.DataFrame:
    """1분봉 → 3분봉. now가 주어지면 아직 끝나지 않은 마지막 봉은 제외한다."""
    if df_1m is None or getattr(df_1m, "empty", True):
        return pd.DataFrame()
    work = df_1m.copy()
    if "datetime" not in work.columns:
        return pd.DataFrame()
    work = work.set_index("datetime")
    bars = (
        work.resample("3min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["close"])
        .reset_index()
    )
    if now is None or bars.empty:
        return bars
    # 봉 시작 + 3분 <= now 인 것만 완성봉
    from datetime import timedelta

    cutoff = now.replace(second=0, microsecond=0) if hasattr(now, "replace") else now
    return bars[bars["datetime"] + timedelta(minutes=3) <= cutoff].reset_index(drop=True)


def _macd_histogram(closes: pd.Series) -> Optional[float]:
    if len(closes) < MACD_SLOW:
        return None
    ema12 = closes.ewm(span=MACD_FAST, adjust=False).mean()
    ema26 = closes.ewm(span=MACD_SLOW, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return float(macd.iloc[-1] - signal.iloc[-1])


def _williams_r(highs: pd.Series, lows: pd.Series, closes: pd.Series) -> Optional[float]:
    if len(closes) < WILLIAMS_PERIOD:
        return None
    hh = highs.rolling(WILLIAMS_PERIOD).max()
    ll = lows.rolling(WILLIAMS_PERIOD).min()
    span = (hh - ll).replace(0.0, float("nan"))
    wr = ((hh - closes) / span * -100.0).dropna()
    return float(wr.iloc[-1]) if len(wr) else None


def confirm_episode_direction(
    df_1m: Optional[pd.DataFrame],
    *,
    proposed_direction: Optional[str],
    now=None,
) -> dict[str, Any]:
    """완성된 3분봉으로 MACD+Williams episode 확인.

    Returns:
      confirmed: bool — proposed_direction과 지표가 정렬되면 True
      indicator_direction: "UP"|"DOWN"|None — 지표가 가리키는 방향
      blocks_enhanced_override: bool — 지표 반대일 때 enhanced가 덮어쓰지 못하게
      macd_hist, williams_r, reason
    """
    empty = {
        "confirmed": False,
        "indicator_direction": None,
        "blocks_enhanced_override": False,
        "macd_hist": None,
        "williams_r": None,
        "reason": "DATA_INSUFFICIENT",
        "broker_order_allowed": False,
    }
    proposed = str(proposed_direction or "").upper()
    bars = resample_completed_3m(df_1m, now=now)
    if len(bars) < MACD_SLOW:
        return empty

    closes = pd.to_numeric(bars["close"], errors="coerce").dropna()
    highs = pd.to_numeric(bars["high"], errors="coerce").dropna() if "high" in bars.columns else closes
    lows = pd.to_numeric(bars["low"], errors="coerce").dropna() if "low" in bars.columns else closes
    hist = _macd_histogram(closes)
    wr = _williams_r(highs, lows, closes)
    if hist is None or wr is None:
        return {**empty, "macd_hist": hist, "williams_r": wr}

    # 같은 방향: MACD hist 부호 + Williams 중립선 기준
    if hist > 0 and wr < WILLIAMS_MID:
        indicator_dir = "UP"
    elif hist < 0 and wr > WILLIAMS_MID:
        indicator_dir = "DOWN"
    else:
        return {
            "confirmed": False,
            "indicator_direction": None,
            "blocks_enhanced_override": False,
            "macd_hist": round(hist, 6),
            "williams_r": round(wr, 4),
            "reason": "MACD_WILLIAMS_DISAGREE",
            "broker_order_allowed": False,
        }

    aligned = proposed in ("UP", "DOWN") and proposed == indicator_dir
    opposite = proposed in ("UP", "DOWN") and proposed != indicator_dir
    return {
        "confirmed": bool(aligned),
        "indicator_direction": indicator_dir,
        "blocks_enhanced_override": bool(opposite or (indicator_dir is not None and proposed not in ("UP", "DOWN"))),
        "macd_hist": round(hist, 6),
        "williams_r": round(wr, 4),
        "reason": "EPISODE_CONFIRMED" if aligned else "EPISODE_OPPOSITE",
        "broker_order_allowed": False,  # 절대 단독 주문 금지
    }


def enhanced_may_set_direction(
    episode_confirm: dict,
    *,
    enhanced_leader: Optional[str],
    live_direction: Optional[str],
) -> bool:
    """enhanced 누적점수가 방향을 덮어쓸 수 있는지.

    MACD+Williams가 명확한 반대 방향이면 False.
    live가 없을 때는 enhanced가 indicator와 같을 때만 허용.
    """
    if not episode_confirm:
        return True
    if not episode_confirm.get("blocks_enhanced_override"):
        return True
    ind = episode_confirm.get("indicator_direction")
    leader = str(enhanced_leader or "").upper()
    if ind in ("UP", "DOWN") and leader in ("UP", "DOWN") and leader != ind:
        return False
    if live_direction not in ("UP", "DOWN"):
        return leader == ind
    return True
