"""time_decay.py — 최근 데이터에 더 높은 가중치를 주는 샘플 weight 계산.

config/ml_training.yaml의 training: 섹션과 1:1 대응한다.
두 방식을 지원한다:
  1) 계단식(tiered) — 최근 30일 3.0, 31~90일 2.0, 91~365일 1.0 (기본)
  2) 지수감쇠(exponential decay) — half_life_days로 감쇠, use_exponential_decay=true일 때
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

import pandas as pd

DEFAULT_RECENT_30D_WEIGHT = 3.0
DEFAULT_RECENT_90D_WEIGHT = 2.0
DEFAULT_OLDER_WEIGHT = 1.0
DEFAULT_DECAY_HALF_LIFE_DAYS = 60


def tiered_weight(age_days: float, recent_30d_weight: float = DEFAULT_RECENT_30D_WEIGHT,
                   recent_90d_weight: float = DEFAULT_RECENT_90D_WEIGHT,
                   older_weight: float = DEFAULT_OLDER_WEIGHT) -> float:
    """age_days: 현재 시점으로부터 며칠 전 데이터인지."""
    if age_days <= 30:
        return recent_30d_weight
    if age_days <= 90:
        return recent_90d_weight
    return older_weight


def exponential_weight(age_days: float, half_life_days: float = DEFAULT_DECAY_HALF_LIFE_DAYS) -> float:
    """half_life_days가 지날 때마다 가중치가 절반이 되는 지수감쇠. age_days=0 -> 1.0."""
    if half_life_days <= 0:
        return 1.0
    return math.pow(0.5, age_days / half_life_days)


def compute_sample_weights(
    timestamps: pd.Series,
    now: Optional[datetime] = None,
    use_exponential_decay: bool = False,
    recent_30d_weight: float = DEFAULT_RECENT_30D_WEIGHT,
    recent_90d_weight: float = DEFAULT_RECENT_90D_WEIGHT,
    older_weight: float = DEFAULT_OLDER_WEIGHT,
    decay_half_life_days: float = DEFAULT_DECAY_HALF_LIFE_DAYS,
) -> pd.Series:
    """timestamps: datetime 시리즈(각 학습 샘플의 시각) -> 동일 인덱스의 weight 시리즈.

    use_exponential_decay=True이면 지수감쇠를, False이면(기본) 계단식 3단계
    가중치를 쓴다. 두 값 모두 config/ml_training.yaml에서 그대로 읽어 전달하면 된다.
    """
    now = now or datetime.now()
    ts = pd.to_datetime(timestamps)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_localize(None)
    age_days = (pd.Timestamp(now) - ts).dt.total_seconds() / 86400.0

    if use_exponential_decay:
        return age_days.apply(lambda a: exponential_weight(a, decay_half_life_days))
    return age_days.apply(lambda a: tiered_weight(a, recent_30d_weight, recent_90d_weight, older_weight))


def recent_window_mask(timestamps: pd.Series, days: int, now: Optional[datetime] = None) -> pd.Series:
    """timestamps 중 최근 days일 이내인 행을 True로 표시하는 boolean mask (성과 별도 집계용)."""
    now = now or datetime.now()
    ts = pd.to_datetime(timestamps)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_localize(None)
    age_days = (pd.Timestamp(now) - ts).dt.total_seconds() / 86400.0
    return age_days <= days
