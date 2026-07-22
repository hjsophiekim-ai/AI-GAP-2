"""strategy_architecture.py — 2026-07-22 단순화 아키텍처 상수·게이트.

역할 분리:
  A (weighted RANGE)  — 유일한 LIVE 실주문 결정자 (진입·비중·청산)
  C (MACD+Williams 3분) — direction_episode 확인기만 (broker 주문 금지)
  D (가격행동 조기진입) — SHADOW 격리 (1분봉 선형보간으로는 활성화 금지)
  E = C 확인 + A 주문   — walk-forward에서 A를 이길 때만 LIVE 게이트 승격

가격행동 정보는 LIVE에서 방향 결정에 쓰지 않고:
  - 진입 시점 5~15초 미세 조정
  - 추격(chase) 여부
  - ETF 방향 확인
에만 사용한다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

# episode 게이트: SHADOW=계산·로그만 / LIVE=진입 차단 활성
# 기본 SHADOW — 20거래일 walk-forward에서 E>A일 때만 LIVE로 승격
EPISODE_GATE_MODE_SHADOW = "SHADOW"
EPISODE_GATE_MODE_LIVE = "LIVE"
DEFAULT_EPISODE_GATE_MODE = EPISODE_GATE_MODE_SHADOW

# 가격행동 조기진입(D)은 항상 SHADOW — LIVE broker 경로 금지
PRICE_ACTION_EARLY_ENTRY_MODE = "SHADOW"

# 진입 타이밍 미세조정 (초)
ENTRY_TIMING_MIN_HELD_SECONDS = 5.0
ENTRY_TIMING_MAX_HELD_SECONDS = 15.0

# 추격 차단 임계 (ETF 이동 %)
CHASE_HARD_BLOCK_PCT = 0.6
# Freshness guard only — discard stale chase signal_price; NOT a chase % threshold.
CHASE_SIGNAL_MAX_AGE_SECONDS = 120.0

STATE_PROMOTION_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data"
    / "state"
    / "strategy_e_promotion.json"
)


def get_episode_gate_mode(state: Optional[dict] = None) -> str:
    """프로덕션 게이트 모드. state override > promotion file > default SHADOW."""
    if state:
        mode = str(state.get("macd_williams_episode_gate_mode") or "").upper()
        if mode in (EPISODE_GATE_MODE_SHADOW, EPISODE_GATE_MODE_LIVE):
            return mode
    try:
        import json

        if STATE_PROMOTION_PATH.exists():
            data = json.loads(STATE_PROMOTION_PATH.read_text(encoding="utf-8"))
            if data.get("promote_e_to_live") is True:
                return EPISODE_GATE_MODE_LIVE
            mode = str(data.get("episode_gate_mode") or "").upper()
            if mode in (EPISODE_GATE_MODE_SHADOW, EPISODE_GATE_MODE_LIVE):
                return mode
    except Exception:
        pass
    return DEFAULT_EPISODE_GATE_MODE


def episode_gate_blocks_entry(mode: str, episode_confirm: dict) -> bool:
    """LIVE 모드에서 episode 미확인/반대면 진입 차단."""
    if mode != EPISODE_GATE_MODE_LIVE:
        return False
    if not episode_confirm:
        return True
    return not bool(episode_confirm.get("confirmed"))


def price_action_may_place_live_order() -> bool:
    """D 가격행동 조기진입은 LIVE 주문 금지."""
    return False


def price_action_shadow_payload(
    *,
    direction: Optional[str],
    factors: dict,
    factor_count: int,
    macd_williams: Optional[dict] = None,
    source: str = "live_5s_tick",
) -> dict[str, Any]:
    """SHADOW 전용 기록 페이로드 — broker 경로에 연결하지 않는다."""
    return {
        "mode": PRICE_ACTION_EARLY_ENTRY_MODE,
        "direction": direction,
        "factors": factors or {},
        "factor_count": int(factor_count or 0),
        "macd_williams": macd_williams or {},
        "source": source,
        "live_order_forbidden": True,
        "note": "1분봉 선형보간 리플레이로는 활성화하지 않음. 실 5초 틱만 SHADOW 기록.",
    }


def entry_timing_ok(held_seconds: Optional[float]) -> tuple[bool, str]:
    """5~15초 미세 조정 창. None이면 통과(데이터 부족 시 fail-open은 호출측 정책)."""
    if held_seconds is None:
        return True, "TIMING_UNKNOWN"
    h = float(held_seconds)
    if h < ENTRY_TIMING_MIN_HELD_SECONDS:
        return False, "TIMING_TOO_EARLY"
    if h > ENTRY_TIMING_MAX_HELD_SECONDS * 4:
        # 이미 충분히 유지된 CONTINUATION은 허용 (미세조정은 조기 진입용)
        return True, "TIMING_CONTINUATION_OK"
    return True, "TIMING_OK"


def chase_hard_block(moved_pct: Optional[float]) -> bool:
    if moved_pct is None:
        return False
    return float(moved_pct) >= CHASE_HARD_BLOCK_PCT


def chase_signal_age_seconds(first_detected_at: Optional[str], now) -> Optional[float]:
    """Age of the current direction_episode chase signal in seconds."""
    if not first_detected_at or now is None:
        return None
    try:
        from datetime import datetime

        detected = datetime.fromisoformat(str(first_detected_at))
        return max(0.0, (now - detected).total_seconds())
    except Exception:
        return None


def signal_price_outside_today_range(
    signal_price: Optional[float],
    df_1min,
    *,
    now=None,
) -> bool:
    """True when signal_price is inconsistent with today's ETF bar range."""
    if signal_price is None or df_1min is None:
        return False
    try:
        import pandas as pd

        work = df_1min
        if work is None or getattr(work, "empty", True):
            return False
        if "datetime" in work.columns and now is not None:
            day = now.strftime("%Y-%m-%d")
            dts = pd.to_datetime(work["datetime"], errors="coerce")
            work = work.loc[dts.dt.strftime("%Y-%m-%d") == day]
            if work.empty:
                return False
        today_high = float(work["high"].max())
        today_low = float(work["low"].min())
        px = float(signal_price)
        # Outside [low, high] → yesterday or wrong-symbol residue.
        return px > today_high or px < today_low
    except Exception:
        return False


def should_discard_stale_chase_signal(
    *,
    first_detected_at: Optional[str],
    signal_price: Optional[float],
    now,
    df_1min=None,
    max_age_seconds: float = CHASE_SIGNAL_MAX_AGE_SECONDS,
) -> tuple[bool, str]:
    """Freshness guard for chase: age>120s or price outside today's ETF range.

    Does not change chase % thresholds — only decides whether to discard and
    re-init the episode signal_price.
    """
    age = chase_signal_age_seconds(first_detected_at, now)
    if age is not None and age > float(max_age_seconds):
        return True, "SIGNAL_AGE_GT_120S"
    if first_detected_at and now is not None:
        try:
            from datetime import datetime

            detected = datetime.fromisoformat(str(first_detected_at))
            if detected.strftime("%Y%m%d") != now.strftime("%Y%m%d"):
                return True, "SIGNAL_FROM_PRIOR_DAY"
        except Exception:
            pass
    if signal_price_outside_today_range(signal_price, df_1min, now=now):
        return True, "SIGNAL_PRICE_OUT_OF_TODAY_RANGE"
    return False, ""
