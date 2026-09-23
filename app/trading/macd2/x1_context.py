"""X1 CONTEXT — 맥락 인식 보조필터 (pure functions only).

이 모듈이 절대 하지 않는 것
---------------------------
* **새 MACD 신호를 만들지 않는다.** 플래그 생성/T+3 confirmation/0교차 판정은
  ``signal_engine`` 의 영역이고, 이 파일은 그 결과를 **입력으로 받기만** 한다.
* 주문/브로커/원장/state 접근을 하지 않는다. 네트워크도 없다.
* 주문금액·수량을 계산하지 않는다 — SMART sizing 은 X1 이 ENTRY/REENTRY/
  LATE_ENTRY 를 허용한 **뒤에** 별도로 돈다.
* N1/C1/H50/SMART/TEG/quality/slot 의 기존 판정값을 바꾸지 않는다. X1 은 그
  판정 **결과를 입력으로 받아** "이 맥락에서 어떻게 다룰지"만 답한다.
* **hard reject 를 절대 되살리지 않는다** (``HARD_REJECT_REASONS``).

4개 하위모듈
------------
X1-1 MORNING CONTEXT     오전 신규진입 승인/감점/WATCH       -> evaluate_morning_context
X1-2 FLIP EXIT           진입 이후 alternating flip 3개 시 조기청산 -> evaluate_flip_exit
X1-3 AFTERNOON RE-ENTRY  오후 동일방향 조건부 재진입(AR1)     -> evaluate_afternoon_reentry
X1-4 FLIP BREAKOUT WATCH soft reject 를 box breakout 으로 살림 -> evaluate_late_entry

미래정보 없음
-------------
모든 함수는 "의사결정 시각 ``now`` 까지 **완성된** 봉"만 본다. 입력 프레임에
그보다 뒤의 봉이 섞여 들어와도 ``_complete_upto`` 가 잘라낸다(방어적) — 호출부
실수로도 future leak 이 생기지 않는다. 마지막 행 이후를 인덱싱하는 코드는 없다.

fail-open
---------
데이터가 모자라면 **기존 N1+C1 동작을 그대로 두는 쪽**으로 답한다. X1 이
데이터 부족을 이유로 정상 거래를 막는 일은 없다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Optional, Sequence, Union

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2.models import Direction

__all__ = [
    "ACTION_PASS", "ACTION_PASS_WEAK", "ACTION_WATCH", "ACTION_EXIT",
    "ACTION_REENTRY", "ACTION_LATE_ENTRY", "ACTION_BLOCK",
    "SOFT_REJECT_REASONS", "HARD_REJECT_REASONS",
    "LIVE_CONFIRMED", "RECOMPUTED_ONLY",
    "X1MorningContext", "X1FlipState", "X1FlipExitDecision",
    "X1ReentryDecision", "X1LateEntryDecision", "X1Context",
    "x1_active", "is_soft_reject", "latency_bucket",
    "premarket_summary", "evaluate_morning_context",
    "evaluate_morning_watch_release", "flip_state_from_live_flags",
    "evaluate_flip_exit", "evaluate_afternoon_reentry",
    "evaluate_flip_watch", "evaluate_late_entry", "afternoon_blue_bonus",
    "build_context",
]

# ── final_action enum ──────────────────────────────────────────────────────
ACTION_PASS = "PASS"
ACTION_PASS_WEAK = "PASS_WEAK"
ACTION_WATCH = "WATCH"
ACTION_EXIT = "EXIT"
ACTION_REENTRY = "REENTRY"
ACTION_LATE_ENTRY = "LATE_ENTRY"
ACTION_BLOCK = "BLOCK"

#: flip sequence 는 **LIVE 확정 원장 이벤트**만 쓴다. 재계산본은 금지다.
LIVE_CONFIRMED = "LIVE_CONFIRMED"
RECOMPUTED_ONLY = "RECOMPUTED_ONLY"

#: 되살릴 수 있는 거절 — 신호 품질/타이밍 판단이라 맥락이 바뀌면 달라질 수 있다.
SOFT_REJECT_REASONS = frozenset({
    "REJECT_NOT_CONFIRMED",
    "REJECT_SHORT_FLAG_INTERVAL",
    "REJECT_MACD_GAP_NOT_EXPANDING",
    "REJECT_LOW_QUALITY_SCORE",
    "TW2_3SLOT_REJECT_QUALITY",
    "TW2_3SLOT_REJECT_TEG",
    config.TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND,
})

#: **절대 되살리지 않는다.** 자본/시간창/브로커/리스크/포지션 충돌 = 안전 차단.
HARD_REJECT_REASONS = frozenset({
    "TW2_3SLOT_REJECT_DAILY_SLOT_CAP",
    "TW2_3SLOT_REJECT_OUTSIDE_WINDOW",
    "REJECT_TIME_WINDOW",
    "REJECT_DUPLICATE_POSITION",
    "REJECT_DAILY_ENTRY_CAP",
    "REJECT_DAY_LOSS_BLOCK",
    "REJECT_INSUFFICIENT_CASH",
    "REJECT_BROKER_UNHEALTHY",
    "REJECT_RECONCILE_UNHEALTHY",
    "REJECT_RISK_BLOCK",
    "FORCED_LIQUIDATION",
})


# ── decision dataclasses ───────────────────────────────────────────────────
@dataclass(frozen=True)
class X1MorningContext:
    score: int = 0
    action: str = ACTION_PASS
    premarket_trend: Optional[str] = None
    session_trend: Optional[str] = None
    components: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    reason: str = ""
    insufficient_data: bool = False


@dataclass(frozen=True)
class X1FlipState:
    #: 창 안 플래그들의 **방향전환 횟수** (X1-4 FLIP WATCH 용).
    flip_count: int = 0
    flag_count: int = 0
    span_min: float = 0.0
    seq: str = ""
    last_direction: Optional[str] = None
    last_flag_at: Optional[datetime] = None
    cluster_start: Optional[datetime] = None
    box_high: Optional[float] = None
    box_low: Optional[float] = None
    watching: bool = False
    source: str = LIVE_CONFIRMED
    # ── 보유 기준 alternating flip (X1-2 FLIP EXIT 전용, 2026-09-23 정정) ──
    #: **진입 이후 새로 발생한 alternating flip flag 의 개수**다. 방향전환
    #: 횟수가 아니다 — RED 보유 중 B(1) -> R(2) -> B(3) 처럼 **첫 반대 플래그가
    #: 1** 이다. ``flip_state_from_live_flags(held_direction=...)`` 를 줘야 채워진다.
    alt_count: int = 0
    alt_seq: str = ""
    alt_last_direction: Optional[str] = None
    alt_first_at: Optional[datetime] = None


@dataclass(frozen=True)
class X1FlipExitDecision:
    armed: bool = False
    exit_now: bool = False
    score: int = 0
    components: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class X1ReentryDecision:
    allowed: bool = False
    stack_exempt: bool = False
    reason: str = ""
    failing_conditions: tuple = ()
    metrics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class X1LateEntryDecision:
    candidate: bool = False
    score: int = 0
    breakout_at: Optional[datetime] = None
    latency_min: Optional[float] = None
    latency_bucket: Optional[str] = None
    within_guard: bool = False
    components: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class X1Context:
    final_action: str = ACTION_PASS
    morning: X1MorningContext = field(default_factory=X1MorningContext)
    flip: X1FlipState = field(default_factory=X1FlipState)
    flip_exit: X1FlipExitDecision = field(default_factory=X1FlipExitDecision)
    reentry: X1ReentryDecision = field(default_factory=X1ReentryDecision)
    late_entry: X1LateEntryDecision = field(default_factory=X1LateEntryDecision)
    afternoon_blue_bonus: bool = False
    context_score: int = 0
    version: str = config.X1_FILTER_VERSION
    reasons: tuple = ()

    def as_trace(self) -> dict:
        """원장/로그에 그대로 실을 평면 dict."""
        return {
            "x1_version": self.version,
            "x1_final_action": self.final_action,
            "x1_context_score": self.context_score,
            "morning_score": self.morning.score,
            "morning_action": self.morning.action,
            "premarket_trend": self.morning.premarket_trend,
            "session_trend": self.morning.session_trend,
            "flip_count": self.flip.flip_count,
            "alt_flip_count": self.flip.alt_count,
            "alt_flip_seq": self.flip.alt_seq,
            "flag_count": self.flip.flag_count,
            "flip_span_min": self.flip.span_min,
            "flip_seq": self.flip.seq,
            "box_high": self.flip.box_high,
            "box_low": self.flip.box_low,
            "box_breakout": bool(self.late_entry.components.get("box_breakout")
                                 or self.flip_exit.components.get("box_break")),
            "flip_exit_armed": self.flip_exit.armed,
            "flip_exit_score": self.flip_exit.score,
            "afternoon_reentry_ar1": self.reentry.allowed,
            "late_entry_score": self.late_entry.score,
            "late_entry_latency_min": self.late_entry.latency_min,
            "late_entry_latency_bucket": self.late_entry.latency_bucket,
            "x1_afternoon_blue_bonus": self.afternoon_blue_bonus,
            "x1_reasons": "|".join(self.reasons),
        }


# ── helpers ────────────────────────────────────────────────────────────────
def _as_direction(value: Union[Direction, str, None]) -> Optional[Direction]:
    if value is None:
        return None
    if isinstance(value, Direction):
        return value if value in (Direction.UP_RED, Direction.DOWN_BLUE) else None
    text = str(value)
    if text in (Direction.UP_RED.value, Direction.DOWN_BLUE.value):
        return Direction(text)
    return None


def _sign(direction: Optional[Direction]) -> float:
    return 1.0 if direction == Direction.UP_RED else -1.0


def _complete_upto(bars: Optional[pd.DataFrame], now: Optional[datetime],
                   *, bar_minutes: int = 3) -> Optional[pd.DataFrame]:
    """``now`` 까지 **완성된** 봉만 남긴다. 미래정보 차단의 단일 지점.

    3분봉은 ``bar_start + bar_minutes <= now`` 일 때만 완성이다. 1분봉이면
    ``bar_minutes=1``. 입력에 더 뒤의 봉이 섞여 있어도 여기서 잘린다.
    """
    if bars is None or len(bars) == 0:
        return None
    if "datetime" not in getattr(bars, "columns", []):
        return None
    work = bars.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    if work["datetime"].dt.tz is None:
        return None
    for col in ("open", "high", "low", "close", "volume"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["datetime", "close"]).sort_values("datetime")
    work = work.drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)
    if now is not None:
        cutoff = pd.Timestamp(now)
        work = work[work["datetime"] + pd.Timedelta(minutes=bar_minutes) <= cutoff]
    work = work.reset_index(drop=True)
    return work if len(work) else None


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _vwap(work: pd.DataFrame) -> Optional[float]:
    if "volume" not in work.columns:
        return None
    sess = work[work["datetime"].dt.time >= config.SESSION_OPEN]
    if sess.empty:
        return None
    vol = pd.to_numeric(sess["volume"], errors="coerce").fillna(0.0)
    if float(vol.sum()) <= 0:
        return None
    hi = sess["high"] if "high" in sess.columns else sess["close"]
    lo = sess["low"] if "low" in sess.columns else sess["close"]
    tp = (pd.to_numeric(hi, errors="coerce") + pd.to_numeric(lo, errors="coerce") + sess["close"]) / 3.0
    return float((tp * vol).sum() / vol.sum())


def _pct(base: Optional[float], value: Optional[float]) -> Optional[float]:
    if base is None or value is None or base <= 0:
        return None
    return (float(value) - float(base)) / float(base) * 100.0


def _parse_hhmm(text: str, fallback: dtime) -> dtime:
    try:
        hh, mm = str(text).split(":")
        return dtime(int(hh), int(mm))
    except Exception:
        return fallback


def _lookback(work: pd.DataFrame, minutes: int) -> pd.DataFrame:
    last = work["datetime"].iloc[-1]
    return work[work["datetime"] >= last - pd.Timedelta(minutes=minutes)]


def x1_active(*, n1_enabled: bool, c1_enabled: bool,
              x1_enabled: Optional[bool] = None) -> bool:
    """X1 은 **N1 과 C1 이 모두 ON** 일 때만 활성화될 수 있다. SMART 와는 독립."""
    flag = config.X1_ENABLED if x1_enabled is None else bool(x1_enabled)
    return bool(flag and n1_enabled and c1_enabled)


def is_soft_reject(reason: Optional[str]) -> bool:
    """soft reject 만 되살릴 수 있다. **모르는 사유는 되살리지 않는다**(fail-safe)."""
    if not reason:
        return False
    if reason in HARD_REJECT_REASONS:
        return False
    return reason in SOFT_REJECT_REASONS


def latency_bucket(minutes: Optional[float]) -> Optional[str]:
    """research trace 용 버킷. production 행동을 막는 것은 별도 guard 다."""
    if minutes is None:
        return None
    for edge in config.X1_LATENCY_BUCKETS_MIN:
        if minutes <= edge:
            return "<=%dm" % edge
    return ">%dm" % config.X1_LATENCY_BUCKETS_MIN[-1]


# ── X1-1 PREMARKET / MORNING CONTEXT ───────────────────────────────────────
def premarket_summary(bars_1m: Optional[pd.DataFrame], now: Optional[datetime]) -> dict:
    """08:00~09:00 프리마켓 요약. 데이터 없으면 빈 dict (fail-open)."""
    work = _complete_upto(bars_1m, now, bar_minutes=1)
    if work is None:
        return {}
    pre = work[work["datetime"].dt.time < config.SESSION_OPEN]
    if pre.empty:
        return {}
    first = float(pre["close"].iloc[0])
    last = float(pre["close"].iloc[-1])
    hi = float(pre["high"].max()) if "high" in pre.columns else float(pre["close"].max())
    lo = float(pre["low"].min()) if "low" in pre.columns else float(pre["close"].min())
    ret = _pct(first, last)
    return {
        "premarket_bars": int(len(pre)),
        "premarket_first": first,
        "premarket_last": last,
        "premarket_return_pct": ret,
        "premarket_range_pct": (None if first <= 0 else (hi - lo) / first * 100.0),
        "premarket_high": hi,
        "premarket_low": lo,
        "premarket_last_direction": (None if ret is None or ret == 0
                                     else (Direction.UP_RED.value if ret > 0
                                           else Direction.DOWN_BLUE.value)),
    }


def evaluate_morning_context(
    bars_3m: Optional[pd.DataFrame],
    bars_1m: Optional[pd.DataFrame],
    direction: Union[Direction, str],
    *,
    now: datetime,
) -> X1MorningContext:
    """오전 플래그가 "큰 흐름을 따르는 신호"인지 "역행 whipsaw"인지 점수화.

    점수는 연구용 초기값이다(config.X1_MORNING_W_*). 새 threshold 최적화 금지.
    데이터가 모자라면 ``insufficient_data=True`` + ``ACTION_PASS`` 로 fail-open.
    """
    d = _as_direction(direction)
    work = _complete_upto(bars_3m, now, bar_minutes=3)
    if d is None or work is None or len(work) < 2:
        return X1MorningContext(action=ACTION_PASS, insufficient_data=True,
                                reason="INSUFFICIENT_BARS")
    s = _sign(d)
    close_now = float(work["close"].iloc[-1])
    metrics: dict[str, Any] = {"close": close_now}
    comp: dict[str, bool] = {}
    score = 0

    pre = premarket_summary(bars_1m, now)
    metrics.update(pre)

    # A. 프리마켓 시작가(없으면 정규장 시가) ~ 현재 = "08:00~현재 주추세"
    day = work[work["datetime"].dt.date == pd.Timestamp(now).date()]
    if day.empty:
        return X1MorningContext(action=ACTION_PASS, insufficient_data=True,
                                reason="NO_SAME_DAY_BARS")
    anchor = pre.get("premarket_first")
    if anchor is None:
        anchor = float(day["close"].iloc[0])
    overall = _pct(anchor, close_now)
    metrics["overall_return_pct"] = overall
    overall_signed = None if overall is None else s * overall
    premarket_trend = None
    if overall is not None and overall != 0:
        premarket_trend = (Direction.UP_RED.value if overall > 0 else Direction.DOWN_BLUE.value)

    if overall_signed is not None and overall_signed > 0:
        comp["premarket_trend_align"] = True
        score += config.X1_MORNING_W_PREMARKET_TREND_ALIGN
    elif overall_signed is not None and overall_signed < 0:
        comp["premarket_trend_against"] = True
        score += config.X1_MORNING_W_PREMARKET_TREND_AGAINST

    # B. 09:00~현재
    sess = day[day["datetime"].dt.time >= config.SESSION_OPEN]
    sess_ret = None
    if not sess.empty:
        sess_ret = _pct(float(sess["close"].iloc[0]), close_now)
    metrics["session_return_pct"] = sess_ret
    session_trend = None
    if sess_ret is not None and sess_ret != 0:
        session_trend = (Direction.UP_RED.value if sess_ret > 0 else Direction.DOWN_BLUE.value)
    if sess_ret is not None and s * sess_ret > 0:
        comp["session_trend_align"] = True
        score += config.X1_MORNING_W_SESSION_TREND_ALIGN

    # C. MACD hist(gap) 방향 확대 — signal_engine 을 읽기만 한다.
    gap_now = gap_prev = None
    try:
        from app.trading.macd2 import signal_engine as _se
        series = _se.calculate_macd_series(work)
        if series is not None and len(series) >= 2:
            gap_now = float(series["macd"].iloc[-1] - series["signal"].iloc[-1])
            gap_prev = float(series["macd"].iloc[-2] - series["signal"].iloc[-2])
    except Exception:
        gap_now = gap_prev = None
    metrics["gap_now"] = gap_now
    metrics["gap_prev"] = gap_prev
    if gap_now is not None and gap_prev is not None and s * (gap_now - gap_prev) > 0:
        comp["gap_expanding"] = True
        score += config.X1_MORNING_W_GAP_EXPANDING

    # EMA20 slope
    closes = work["close"].astype(float)
    ema20 = _ema(closes, 20)
    slope20 = float(ema20.iloc[-1] - ema20.iloc[-2]) if len(ema20) >= 2 else None
    metrics["ema20_slope"] = slope20
    if slope20 is not None and s * slope20 > 0:
        comp["ema20_slope_align"] = True
        score += config.X1_MORNING_W_EMA20_SLOPE_ALIGN

    # VWAP
    vwap = _vwap(day)
    metrics["vwap"] = vwap
    if vwap is not None and s * (close_now - vwap) > 0:
        comp["vwap_favorable"] = True
        score += config.X1_MORNING_W_VWAP_FAVORABLE

    # D. 짧은 반전만 vs 큰 추세 반대
    short = _lookback(day, config.X1_MORNING_SHORT_LOOKBACK_MIN)
    long_ = _lookback(day, config.X1_MORNING_LONG_LOOKBACK_MIN)
    short_ret = _pct(float(short["close"].iloc[0]), close_now) if len(short) >= 2 else None
    long_ret = _pct(float(long_["close"].iloc[0]), close_now) if len(long_) >= 2 else None
    metrics["short_return_pct"] = short_ret
    metrics["long_return_pct"] = long_ret
    if (short_ret is not None and long_ret is not None
            and s * short_ret > 0 and s * long_ret < 0):
        comp["short_reversal_only"] = True
        score += config.X1_MORNING_W_SHORT_REVERSAL_ONLY

    # 당일 range 중앙의 짧은 되돌림
    hi = float(day["high"].max()) if "high" in day.columns else float(day["close"].max())
    lo = float(day["low"].min()) if "low" in day.columns else float(day["close"].min())
    pctl = None
    if hi > lo:
        pctl = (close_now - lo) / (hi - lo)
    metrics["day_range_percentile"] = pctl
    if (pctl is not None
            and config.X1_MORNING_MIDRANGE_LO_PCTL <= pctl <= config.X1_MORNING_MIDRANGE_HI_PCTL):
        comp["midrange_pullback"] = True
        score += config.X1_MORNING_W_MIDRANGE_PULLBACK

    if score >= config.X1_MORNING_PASS_SCORE:
        action, reason = ACTION_PASS, "X1_MORNING_PASS"
    elif score >= 0:
        action, reason = ACTION_PASS_WEAK, "X1_WEAK_CONTEXT"
    else:
        action, reason = ACTION_WATCH, "X1_MORNING_WATCH"

    return X1MorningContext(
        score=int(score), action=action, premarket_trend=premarket_trend,
        session_trend=session_trend, components=comp, metrics=metrics, reason=reason,
    )


def evaluate_morning_watch_release(
    bars_3m: Optional[pd.DataFrame],
    direction: Union[Direction, str],
    *,
    now: datetime,
    watch_started_at: datetime,
    box_high: Optional[float] = None,
    box_low: Optional[float] = None,
) -> tuple[bool, str, dict]:
    """WATCH 해제 판정. 원래 flag 방향으로 swing/box breakout 이 나오면 재승인.

    ``X1_MORNING_WATCH_MAX_MIN`` 을 넘기면 폐기(재승인 불가). 미래정보 없음 —
    ``now`` 까지 완성된 봉만 본다.
    """
    d = _as_direction(direction)
    work = _complete_upto(bars_3m, now, bar_minutes=3)
    if d is None or work is None:
        return False, "INSUFFICIENT_BARS", {}
    elapsed = (pd.Timestamp(now) - pd.Timestamp(watch_started_at)).total_seconds() / 60.0
    if elapsed > config.X1_MORNING_WATCH_MAX_MIN:
        return False, "X1_WATCH_EXPIRED", {"elapsed_min": elapsed}
    prior = work[work["datetime"] < pd.Timestamp(watch_started_at)]
    if box_high is None:
        box_high = float(prior["high"].max()) if len(prior) and "high" in prior.columns else None
    if box_low is None:
        box_low = float(prior["low"].min()) if len(prior) and "low" in prior.columns else None
    close_now = float(work["close"].iloc[-1])
    metrics = {"elapsed_min": elapsed, "box_high": box_high, "box_low": box_low,
               "close": close_now}
    if d == Direction.UP_RED and box_high is not None and close_now > box_high:
        return True, "X1_WATCH_RELEASED_BREAKOUT", metrics
    if d == Direction.DOWN_BLUE and box_low is not None and close_now < box_low:
        return True, "X1_WATCH_RELEASED_BREAKOUT", metrics
    return False, "X1_WATCH_HOLDING", metrics


# ── X1-2 / X1-4 공통: LIVE 원장 기준 flip sequence ─────────────────────────
def flip_state_from_live_flags(
    flag_events: Optional[Sequence[dict]],
    *,
    now: datetime,
    since: Optional[datetime] = None,
    window_minutes: Optional[int] = None,
    bars_3m: Optional[pd.DataFrame] = None,
    held_direction: Union[Direction, str, None] = None,
) -> X1FlipState:
    """**LIVE_CONFIRMED 원장 이벤트만**으로 flip sequence 를 만든다.

    ``flag_events`` 각 원소는 최소 ``{"at": tz-aware datetime,
    "direction": "UP_RED"|"DOWN_BLUE", "source": "LIVE_CONFIRMED"}``.
    ``source`` 가 ``RECOMPUTED_ONLY`` 인 것은 **버린다**(사양 §9).

    ``flip_count`` 는 "플래그 개수"가 아니라 **방향전환 횟수**다 (X1-4 용).
    box 는 cluster 구간 3분봉의 high/low (``bars_3m`` 이 있을 때만).

    ``held_direction`` 을 주면 X1-2 FLIP EXIT 용 **alternating flip count** 를
    함께 계산한다(2026-09-23 사용자 정정):

        count = 0 에서 시작
        진입 이후 **최초 반대방향** 플래그 -> count = 1
        이후 직전 counted 플래그와 방향이 바뀔 때마다 +1
        같은 방향 반복은 증가 없음

    즉 RED 보유 중 B, R, B 면 ``alt_count == 3`` 이다(방향전환 횟수는 2).
    """
    if not flag_events:
        return X1FlipState()
    rows = []
    for e in flag_events:
        if not isinstance(e, dict):
            continue
        if str(e.get("source") or LIVE_CONFIRMED) != LIVE_CONFIRMED:
            continue
        d = _as_direction(e.get("direction"))
        at = e.get("at")
        if d is None or at is None:
            continue
        ts = pd.Timestamp(at)
        if ts.tzinfo is None:
            continue
        if ts > pd.Timestamp(now):
            continue                      # 미래 이벤트 차단
        if since is not None and ts < pd.Timestamp(since):
            continue
        if window_minutes is not None and ts < pd.Timestamp(now) - pd.Timedelta(minutes=window_minutes):
            continue
        rows.append((ts, d))
    if not rows:
        return X1FlipState()
    rows.sort(key=lambda r: r[0])

    flips = 0
    for i in range(1, len(rows)):
        if rows[i][1] != rows[i - 1][1]:
            flips += 1
    seq = "".join("R" if d == Direction.UP_RED else "B" for _, d in rows)
    span = (rows[-1][0] - rows[0][0]).total_seconds() / 60.0

    box_hi = box_lo = None
    work = _complete_upto(bars_3m, now, bar_minutes=3)
    if work is not None:
        seg = work[(work["datetime"] >= rows[0][0]) & (work["datetime"] <= rows[-1][0])]
        if len(seg):
            if "high" in seg.columns:
                box_hi = float(seg["high"].max())
            if "low" in seg.columns:
                box_lo = float(seg["low"].min())

    # ── alternating flip count (보유방향 기준) ──────────────────────────
    held = _as_direction(held_direction)
    alt_count = 0
    alt_seq = ""
    alt_last: Optional[Direction] = None
    alt_first_at: Optional[datetime] = None
    if held is not None:
        for ts, d in rows:
            if alt_last is None:
                if d != held:                      # 최초 **반대** 플래그가 1
                    alt_count = 1
                    alt_last = d
                    alt_first_at = ts.to_pydatetime()
                    alt_seq = "R" if d == Direction.UP_RED else "B"
                # 최초 반대 플래그 전의 동일방향 플래그는 세지 않는다
            elif d != alt_last:                    # 직전 counted 와 달라질 때만 +1
                alt_count += 1
                alt_last = d
                alt_seq += "R" if d == Direction.UP_RED else "B"
            # 같은 방향 반복은 증가 없음

    return X1FlipState(
        flip_count=flips, flag_count=len(rows), span_min=span, seq=seq,
        last_direction=rows[-1][1].value, last_flag_at=rows[-1][0].to_pydatetime(),
        cluster_start=rows[0][0].to_pydatetime(), box_high=box_hi, box_low=box_lo,
        source=LIVE_CONFIRMED,
        alt_count=alt_count, alt_seq=alt_seq,
        alt_last_direction=(alt_last.value if alt_last is not None else None),
        alt_first_at=alt_first_at,
    )


# ── X1-2 FLIP EXIT ─────────────────────────────────────────────────────────
def evaluate_flip_exit(
    bars_3m: Optional[pd.DataFrame],
    held_direction: Union[Direction, str],
    flip: X1FlipState,
    *,
    now: datetime,
    h50_hold_seen: bool = False,
    etf_move_pct: Optional[float] = None,
    already_armed: bool = False,
) -> X1FlipExitDecision:
    """보유 이후 alternating flip 이 쌓이면 늦기 전에 전량청산.

    **ARM 은 "청산 판정 시점"이 아니라 "감시 시작 시점"이다** (2026-09-23 사용자
    정정). 호출부는 ARM 이 한 번 성립하면 ``already_armed=True`` 로 **완성 3분봉
    마다** 이 함수를 다시 부르고, ``exit_now`` 가 처음 True 가 되는 봉에서
    전량청산한다. ARM 판정 자체는 그 뒤 플래그 방향이 어떻게 되든 유지된다.

    **기존 청산(H50 / STOP / OPPOSITE_SIGNAL / 강제청산)이 먼저 나가면 호출부가
    감시를 즉시 끝낸다** — X1 은 기존 청산을 지연시키지 않는다. 실제 청산 이후
    시점에는 절대 평가하지 않는다(호출부 계약).

    ARM 조건 (2026-09-23 사용자 정정):
      1. 보유 중이고
      2. **포지션 진입 이후 새로 발생한 alternating flip flag** 가
         ``X1_FLIP_EXIT_MIN_FLIPS`` 개 이상이며 (``flip.alt_count``) —
         **방향전환 횟수가 아니다.** RED 보유 중 B(1) -> R(2) -> B(3) 처럼
         **첫 반대 플래그가 1** 이다. 호출부가 ``since=진입시각`` +
         ``held_direction`` 으로 만들어 넘긴다(청산하면 다음 포지션에서 0)
      3. **마지막 플래그가 보유방향의 반대**일 것.

    **H50 HOLD 는 더 이상 필요조건이 아니다** (``X1_FLIP_EXIT_REQUIRE_H50``,
    기본 False). H50 이 이미 HOLD 중이면 우선순위 신호로만 기록한다
    (``components["h50_priority"]``) — 점수에는 넣지 않는다. 이전 구현은 H50
    HOLD 를 필수로 봐서 2026-09-22 09:06 RED 처럼 HOLD 이후 플래그가 0개인
    사례에서 영원히 ARM 되지 않았다.

    3회 도달만으로 즉시청산하지 않는다 — 아래 확인조건 점수가
    ``X1_FLIP_EXIT_SCORE_MIN`` 이상일 때만 청산한다.
    자동 reverse 는 하지 않는다 — 청산만 답한다(사양 §2).

    ``etf_move_pct`` 는 호출부가 보유 ETF 가격에서 계산한 **반대방향 추종률(%)**
    이다(양수면 반대방향으로 따라가는 중). 없으면 그 항목만 0점(fail-open).
    """
    held = _as_direction(held_direction)
    if held is None:
        return X1FlipExitDecision(reason="NO_DIRECTION")
    # 이미 ARM 된 포지션은 게이트를 다시 통과시키지 않는다 — 감시만 계속한다.
    if already_armed:
        return _score_flip_exit(bars_3m, held, flip, now=now,
                                h50_hold_seen=h50_hold_seen,
                                etf_move_pct=etf_move_pct, rearmed=True)
    if config.X1_FLIP_EXIT_REQUIRE_H50 and not h50_hold_seen:
        return X1FlipExitDecision(reason="X1_FLIP_EXIT_NOT_ARMED_NO_H50_HOLD")
    if flip.alt_count < config.X1_FLIP_EXIT_MIN_FLIPS:
        return X1FlipExitDecision(
            reason="X1_FLIP_EXIT_NOT_ARMED_FLIPS_%d" % flip.alt_count,
            metrics={"alt_count": flip.alt_count, "alt_seq": flip.alt_seq,
                     "flip_count": flip.flip_count,
                     "h50_hold_seen": bool(h50_hold_seen)})
    last_flag = _as_direction(flip.alt_last_direction or flip.last_direction)
    if last_flag is None or last_flag == held:
        # 마지막 플래그가 보유방향과 같으면 "추세가 내 쪽으로 돌아온" 상태다.
        return X1FlipExitDecision(
            reason="X1_FLIP_EXIT_LAST_FLAG_SAME_DIRECTION",
            metrics={"alt_count": flip.alt_count, "alt_seq": flip.alt_seq,
                     "flip_count": flip.flip_count,
                     "last_direction": flip.alt_last_direction or flip.last_direction,
                     "held_direction": held.value,
                     "h50_hold_seen": bool(h50_hold_seen)})

    return _score_flip_exit(bars_3m, held, flip, now=now,
                            h50_hold_seen=h50_hold_seen,
                            etf_move_pct=etf_move_pct, rearmed=False)


def _score_flip_exit(
    bars_3m: Optional[pd.DataFrame],
    held: Direction,
    flip: X1FlipState,
    *,
    now: datetime,
    h50_hold_seen: bool,
    etf_move_pct: Optional[float],
    rearmed: bool,
) -> X1FlipExitDecision:
    """ARM 성립 이후의 확인조건 채점. ARM 판정은 호출부가 이미 끝냈다."""
    work = _complete_upto(bars_3m, now, bar_minutes=3)
    if work is None or len(work) < 3:
        return X1FlipExitDecision(armed=True, reason="INSUFFICIENT_BARS")

    opp = Direction.DOWN_BLUE if held == Direction.UP_RED else Direction.UP_RED
    so = _sign(opp)
    close_now = float(work["close"].iloc[-1])
    comp: dict[str, bool] = {}
    metrics: dict[str, Any] = {"close": close_now, "alt_count": flip.alt_count,
                               "alt_seq": flip.alt_seq, "flip_count": flip.flip_count,
                               "flip_seq": flip.seq, "box_high": flip.box_high,
                               "box_low": flip.box_low,
                               "flip_since": (flip.cluster_start.isoformat()
                                              if flip.cluster_start else None),
                               "last_direction": flip.last_direction,
                               "held_direction": held.value,
                               "h50_hold_seen": bool(h50_hold_seen)}
    # H50 이 이미 HOLD 중이면 우선순위 신호로만 남긴다 — 점수에는 넣지 않는다.
    comp["h50_priority"] = bool(h50_hold_seen)
    score = 0

    # A. 완성 3분봉 종가가 flip 구간 반대방향 swing 을 돌파
    box_break = False
    if opp == Direction.UP_RED and flip.box_high is not None:
        box_break = close_now > flip.box_high
    elif opp == Direction.DOWN_BLUE and flip.box_low is not None:
        box_break = close_now < flip.box_low
    comp["box_break"] = bool(box_break)
    if box_break:
        score += config.X1_EXIT_W_BOX_BREAK

    # B. MACD gap 이 반대방향으로 확대
    try:
        from app.trading.macd2 import signal_engine as _se
        series = _se.calculate_macd_series(work)
    except Exception:
        series = None
    if series is not None and len(series) >= 2:
        g0 = float(series["macd"].iloc[-1] - series["signal"].iloc[-1])
        g1 = float(series["macd"].iloc[-2] - series["signal"].iloc[-2])
        metrics["gap_now"], metrics["gap_prev"] = g0, g1
        if so * (g0 - g1) > 0:
            comp["gap_expanding_opposite"] = True
            score += config.X1_EXIT_W_GAP_EXPANDING

    # EMA10/20 slope 반전
    closes = work["close"].astype(float)
    e10, e20 = _ema(closes, 10), _ema(closes, 20)
    if len(e10) >= 2 and len(e20) >= 2:
        s10 = float(e10.iloc[-1] - e10.iloc[-2])
        s20 = float(e20.iloc[-1] - e20.iloc[-2])
        metrics["ema10_slope"], metrics["ema20_slope"] = s10, s20
        if so * s10 > 0 and so * s20 > 0:
            comp["ema_slope_flipped"] = True
            score += config.X1_EXIT_W_EMA_SLOPE_FLIP

    # ETF 가 반대방향을 추종
    if etf_move_pct is not None:
        metrics["etf_move_pct"] = float(etf_move_pct)
        if float(etf_move_pct) > 0:
            comp["etf_follows_opposite"] = True
            score += config.X1_EXIT_W_ETF_FOLLOW

    # C. 기존 방향이 직전 extreme 갱신 실패
    seg = work
    if flip.cluster_start is not None:
        seg = work[work["datetime"] >= pd.Timestamp(flip.cluster_start)]
    extreme_fail = False
    if len(seg) >= 2:
        if held == Direction.UP_RED and "high" in seg.columns:
            extreme_fail = float(seg["high"].iloc[-1]) < float(seg["high"].iloc[:-1].max())
        elif held == Direction.DOWN_BLUE and "low" in seg.columns:
            extreme_fail = float(seg["low"].iloc[-1]) > float(seg["low"].iloc[:-1].min())
    comp["held_extreme_fail"] = bool(extreme_fail)
    if extreme_fail:
        score += config.X1_EXIT_W_EXTREME_FAIL

    exit_now = score >= config.X1_FLIP_EXIT_SCORE_MIN
    metrics["rearmed"] = bool(rearmed)
    return X1FlipExitDecision(
        armed=True, exit_now=bool(exit_now), score=int(score), components=comp,
        metrics=metrics,
        reason=("X1_FLIP_EXIT" if exit_now
                else ("X1_FLIP_EXIT_WATCHING_SCORE_%d" % score if rearmed
                      else "X1_FLIP_EXIT_SCORE_%d" % score)),
    )


# ── X1-3 AFTERNOON SAME-DIRECTION RE-ENTRY (AR1) ───────────────────────────
def evaluate_afternoon_reentry(
    teg_decision: Any,
    *,
    base_reject_reason: Optional[str],
    enabled: Optional[bool] = None,
) -> X1ReentryDecision:
    """AR1 — **narrow exception only**. 새 numerical threshold 를 만들지 않는다.

    적용대상은 ``SAME_DIRECTION_AFTERNOON`` 으로 거절된 후보뿐이다. TEG 는 그대로
    요구하되, **탈락 조건이 ``price_ema_stack_aligned`` 하나뿐이고** 동시에
    ``macd_gap_signed_net_expanding`` / ``ema_spread_signed_net_expanding`` /
    ``vwap_favorable_side`` 가 전부 참이면 stack 만 면제한다.

    PLB(오후 TEG 전체 stack 면제)는 이 함수가 할 수 없다 — 호출부가
    base_reject_reason 을 SAME_DIRECTION_AFTERNOON 으로 넘길 때만 동작한다.
    """
    on = config.X1_AR1_ENABLED if enabled is None else bool(enabled)
    if not on:
        return X1ReentryDecision(reason="X1_AR1_DISABLED")
    if base_reject_reason != config.TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND:
        return X1ReentryDecision(reason="X1_AR1_OUT_OF_SCOPE")

    from app.trading.macd2 import teg_gate as _teg
    conditions = dict(getattr(teg_decision, "conditions", None) or {})
    if not conditions:
        return X1ReentryDecision(reason="X1_AR1_NO_TEG_CONDITIONS")
    if bool(getattr(teg_decision, "approved", False)):
        return X1ReentryDecision(allowed=True, stack_exempt=False,
                                 reason="X1_AR1_TEG_ALREADY_APPROVED",
                                 metrics=dict(getattr(teg_decision, "metrics", None) or {}))

    failing = tuple(c for c in _teg.ALL_CONDITIONS if not conditions.get(c, False))
    metrics = dict(getattr(teg_decision, "metrics", None) or {})
    if failing != (_teg.COND_EMA_STACK,):
        return X1ReentryDecision(reason="X1_AR1_MULTI_CONDITION_FAIL",
                                 failing_conditions=failing, metrics=metrics)
    if not (conditions.get(_teg.COND_MACD_GAP_EXPANDING)
            and conditions.get(_teg.COND_EMA_SPREAD_EXPANDING)
            and conditions.get(_teg.COND_VWAP)):
        return X1ReentryDecision(reason="X1_AR1_MOMENTUM_NOT_CONFIRMED",
                                 failing_conditions=failing, metrics=metrics)
    return X1ReentryDecision(allowed=True, stack_exempt=True,
                             reason="X1_AR1_STACK_EXEMPT",
                             failing_conditions=failing, metrics=metrics)


# ── X1-4 FLIP BREAKOUT WATCH / LATE ENTRY ──────────────────────────────────
def evaluate_flip_watch(
    flag_events: Optional[Sequence[dict]],
    *,
    now: datetime,
    bars_3m: Optional[pd.DataFrame] = None,
) -> X1FlipState:
    """WATCH 진입 여부. **두 조건 중 하나**만 맞아도 WATCH (짧은 창 하나로 제한 금지).

    A: 최근 ``X1_FLIPWATCH_WINDOW_A_MIN`` 분 동안 방향전환 >= ``..MIN_FLIPS_A``
    B: 최근 ``X1_FLIPWATCH_WINDOW_B_MIN`` 분 동안 플래그 >= ``..MIN_FLAGS_B``

    B 가 더 긴 창이므로 9/22 오후 RBRBRB(66분) 같은 긴 congestion 도 잡힌다.
    """
    a = flip_state_from_live_flags(flag_events, now=now, bars_3m=bars_3m,
                                   window_minutes=config.X1_FLIPWATCH_WINDOW_A_MIN)
    b = flip_state_from_live_flags(flag_events, now=now, bars_3m=bars_3m,
                                   window_minutes=config.X1_FLIPWATCH_WINDOW_B_MIN)
    hit_a = a.flip_count >= config.X1_FLIPWATCH_MIN_FLIPS_A
    hit_b = b.flag_count >= config.X1_FLIPWATCH_MIN_FLAGS_B
    if not (hit_a or hit_b):
        return X1FlipState(flip_count=b.flip_count, flag_count=b.flag_count,
                           span_min=b.span_min, seq=b.seq,
                           last_direction=b.last_direction, last_flag_at=b.last_flag_at,
                           cluster_start=b.cluster_start, box_high=b.box_high,
                           box_low=b.box_low, watching=False)
    chosen = b if hit_b else a
    return X1FlipState(flip_count=chosen.flip_count, flag_count=chosen.flag_count,
                       span_min=chosen.span_min, seq=chosen.seq,
                       last_direction=chosen.last_direction,
                       last_flag_at=chosen.last_flag_at,
                       cluster_start=chosen.cluster_start, box_high=chosen.box_high,
                       box_low=chosen.box_low, watching=True)


def evaluate_late_entry(
    bars_3m: Optional[pd.DataFrame],
    flip: X1FlipState,
    *,
    now: datetime,
    base_reject_reason: Optional[str],
    etf_move_pct: Optional[float] = None,
) -> X1LateEntryDecision:
    """soft reject 된 신호를 cluster box breakout 으로 늦게라도 살린다.

    **hard reject 는 절대 되살리지 않는다.** breakout 시각/지연/버킷을 전부
    기록하고, production 행동만 ``X1_LATE_ENTRY_MAX_LATENCY_MIN`` provisional
    guard 로 막는다(``within_guard``).
    """
    if not is_soft_reject(base_reject_reason):
        # hard reject 와 "목록에 없는 사유" 를 구분해서 남긴다 — 둘 다 되살리지
        # 않는 것은 같지만, 후자는 SOFT 목록 누락 가능성을 사후에 찾기 위한 신호다.
        reason = ("X1_LATE_HARD_REJECT_NOT_RESURRECTED"
                  if base_reject_reason in HARD_REJECT_REASONS
                  else "X1_LATE_UNLISTED_REASON_NOT_RESURRECTED")
        return X1LateEntryDecision(reason=reason,
                                   metrics={"base_reject_reason": base_reject_reason or ""})
    if not flip.watching or flip.last_direction is None:
        return X1LateEntryDecision(reason="X1_LATE_NO_FLIP_CLUSTER")
    d = _as_direction(flip.last_direction)
    work = _complete_upto(bars_3m, now, bar_minutes=3)
    if d is None or work is None or len(work) < 2:
        return X1LateEntryDecision(reason="INSUFFICIENT_BARS")
    if flip.box_high is None or flip.box_low is None:
        return X1LateEntryDecision(reason="X1_LATE_NO_BOX")

    s = _sign(d)
    close_now = float(work["close"].iloc[-1])
    bar_now = work["datetime"].iloc[-1]
    comp: dict[str, bool] = {}
    metrics: dict[str, Any] = {"close": close_now, "box_high": flip.box_high,
                               "box_low": flip.box_low, "flip_seq": flip.seq,
                               "flip_count": flip.flip_count,
                               "cluster_span_min": flip.span_min}
    score = 0

    breakout = (close_now > flip.box_high) if d == Direction.UP_RED else (close_now < flip.box_low)
    comp["box_breakout"] = bool(breakout)
    if breakout:
        score += config.X1_LATE_W_BOX_BREAKOUT

    try:
        from app.trading.macd2 import signal_engine as _se
        series = _se.calculate_macd_series(work)
    except Exception:
        series = None
    if series is not None and len(series) >= 2:
        g0 = float(series["macd"].iloc[-1] - series["signal"].iloc[-1])
        g1 = float(series["macd"].iloc[-2] - series["signal"].iloc[-2])
        metrics["gap_now"], metrics["gap_prev"] = g0, g1
        if s * (g0 - g1) > 0:
            comp["gap_expanding"] = True
            score += config.X1_LATE_W_GAP_EXPANDING

    # next-bar hold: **직전 봉도 box 밖**이었는지로 판정한다(미래봉을 보지 않는다).
    if len(work) >= 2:
        prev_close = float(work["close"].iloc[-2])
        prev_out = (prev_close > flip.box_high) if d == Direction.UP_RED else (prev_close < flip.box_low)
        comp["next_bar_hold"] = bool(breakout and prev_out)
        if comp["next_bar_hold"]:
            score += config.X1_LATE_W_NEXT_BAR_HOLD

    if etf_move_pct is not None:
        metrics["etf_move_pct"] = float(etf_move_pct)
        if float(etf_move_pct) > 0:
            comp["etf_confirm"] = True
            score += config.X1_LATE_W_ETF_CONFIRM

    closes = work["close"].astype(float)
    e10, e20 = _ema(closes, 10), _ema(closes, 20)
    if len(e10) >= 2 and len(e20) >= 2:
        spread_now = float(e10.iloc[-1] - e20.iloc[-1])
        spread_prev = float(e10.iloc[-2] - e20.iloc[-2])
        metrics["ema_spread_now"], metrics["ema_spread_prev"] = spread_now, spread_prev
        if s * (spread_now - spread_prev) > 0:
            comp["ema_spread_expanding"] = True
            score += config.X1_LATE_W_EMA_SPREAD

    day = work[work["datetime"].dt.date == pd.Timestamp(now).date()]
    vwap = _vwap(day) if len(day) else None
    metrics["vwap"] = vwap
    if vwap is not None and s * (close_now - vwap) > 0:
        comp["vwap_favorable"] = True
        score += config.X1_LATE_W_VWAP

    lat = None
    if flip.last_flag_at is not None:
        lat = (pd.Timestamp(bar_now) - pd.Timestamp(flip.last_flag_at)).total_seconds() / 60.0
    bucket = latency_bucket(lat)
    within = (lat is not None and lat <= config.X1_LATE_ENTRY_MAX_LATENCY_MIN)
    candidate = bool(breakout and score >= config.X1_LATE_ENTRY_SCORE_MIN)

    return X1LateEntryDecision(
        candidate=candidate, score=int(score),
        breakout_at=(pd.Timestamp(bar_now).to_pydatetime() if breakout else None),
        latency_min=lat, latency_bucket=bucket, within_guard=bool(within),
        components=comp, metrics=metrics,
        reason=("X1_LATE_ENTRY" if candidate and within else
                "X1_LATE_ENTRY_OUT_OF_GUARD" if candidate else
                "X1_LATE_SCORE_%d" % score),
    )


# ── X1-5 AFTERNOON BLUE BONUS ──────────────────────────────────────────────
def afternoon_blue_bonus(
    bars_3m: Optional[pd.DataFrame],
    direction: Union[Direction, str],
    *,
    now: datetime,
) -> tuple[bool, dict]:
    """13시 이후 BLUE 가점(+1). **무조건 주지 않는다** — 아래가 전부 참일 때만.

    * 09:00~12:00 dominant direction == RED
    * 13:00 이후 BLUE
    * BLUE 방향 MACD gap 확대
    * 현재가 < VWAP
    * 직전 swing low 하향돌파 **또는** lower-high 구조
    """
    d = _as_direction(direction)
    work = _complete_upto(bars_3m, now, bar_minutes=3)
    if d != Direction.DOWN_BLUE or work is None or len(work) < 3:
        return False, {}
    after = _parse_hhmm(config.X1_AFTERNOON_BLUE_AFTER, dtime(13, 0))
    t_now = pd.Timestamp(now).time()
    if t_now < after:
        return False, {"reason": "BEFORE_CUTOFF"}

    day = work[work["datetime"].dt.date == pd.Timestamp(now).date()]
    if day.empty:
        return False, {"reason": "NO_DAY_BARS"}
    m_start = _parse_hhmm(config.X1_MORNING_DOMINANT_START, dtime(9, 0))
    m_end = _parse_hhmm(config.X1_MORNING_DOMINANT_END, dtime(12, 0))
    morning = day[(day["datetime"].dt.time >= m_start) & (day["datetime"].dt.time < m_end)]
    if len(morning) < 2:
        return False, {"reason": "NO_MORNING_BARS"}
    morning_ret = _pct(float(morning["close"].iloc[0]), float(morning["close"].iloc[-1]))
    metrics: dict[str, Any] = {"morning_return_pct": morning_ret}
    if morning_ret is None or morning_ret <= 0:
        return False, {**metrics, "reason": "MORNING_NOT_RED"}

    try:
        from app.trading.macd2 import signal_engine as _se
        series = _se.calculate_macd_series(work)
    except Exception:
        series = None
    if series is None or len(series) < 2:
        return False, {**metrics, "reason": "NO_MACD"}
    g0 = float(series["macd"].iloc[-1] - series["signal"].iloc[-1])
    g1 = float(series["macd"].iloc[-2] - series["signal"].iloc[-2])
    metrics["gap_now"], metrics["gap_prev"] = g0, g1
    if not (g0 - g1) < 0:
        return False, {**metrics, "reason": "GAP_NOT_EXPANDING_BLUE"}

    close_now = float(day["close"].iloc[-1])
    vwap = _vwap(day)
    metrics["close"], metrics["vwap"] = close_now, vwap
    if vwap is None or close_now >= vwap:
        return False, {**metrics, "reason": "NOT_BELOW_VWAP"}

    prior = day.iloc[:-1]
    swing_low = float(prior["low"].min()) if len(prior) and "low" in prior.columns else None
    lower_high = False
    if len(prior) >= 3 and "high" in prior.columns:
        lower_high = float(day["high"].iloc[-1]) < float(prior["high"].iloc[-3:].max())
    metrics["swing_low"] = swing_low
    metrics["lower_high"] = lower_high
    broke_low = swing_low is not None and close_now < swing_low
    metrics["broke_swing_low"] = broke_low
    if not (broke_low or lower_high):
        return False, {**metrics, "reason": "NO_STRUCTURE"}
    return True, {**metrics, "reason": "X1_AFTERNOON_BLUE_BONUS"}


# ── orchestrator ───────────────────────────────────────────────────────────
def build_context(
    *,
    now: datetime,
    direction: Union[Direction, str, None] = None,
    bars_3m: Optional[pd.DataFrame] = None,
    bars_1m: Optional[pd.DataFrame] = None,
    flag_events: Optional[Sequence[dict]] = None,
    base_approved: bool = False,
    base_reject_reason: Optional[str] = None,
    teg_decision: Any = None,
    held_direction: Union[Direction, str, None] = None,
    h50_hold_seen: bool = False,
    flip_exit_armed: bool = False,
    flip_since: Optional[datetime] = None,
    etf_move_pct: Optional[float] = None,
    is_morning: Optional[bool] = None,
) -> X1Context:
    """네 모듈을 한 번에 돌려 하나의 trace 로 묶는다. **순수 함수** — 주문 없음.

    이 함수는 아무것도 실행하지 않는다. ``final_action`` 은 "호출부가 이렇게
    해도 된다"는 **권고**일 뿐이고, 단계 1 에서는 shadow 기록에만 쓰인다.
    """
    reasons: list[str] = []
    d = _as_direction(direction)

    if is_morning is None:
        is_morning = pd.Timestamp(now).time() < config.TW2_3SLOT_MORNING_WINDOW_END

    # 보유 중이면 FLIP EXIT 이 최우선.
    # flip 은 **현재 포지션 진입 이후**부터 센다(2026-09-23 사용자 정정) —
    # ``flip_since`` 에 **진입시각**을 넘긴다. 청산하면 다음 포지션에서 0 부터
    # 다시 센다(호출부가 새 진입시각을 넘기므로 자동 reset). 넘기지 않으면
    # 당일 전체를 세므로 과다 카운트가 된다 — 보유 판정에는 반드시 넘길 것.
    held = _as_direction(held_direction)
    flip_all = flip_state_from_live_flags(flag_events, now=now, bars_3m=bars_3m)
    exit_dec = X1FlipExitDecision()
    if held is not None:
        flip_pos = flip_state_from_live_flags(flag_events, now=now, since=flip_since,
                                              bars_3m=bars_3m, held_direction=held)
        flip_all = flip_pos
        exit_dec = evaluate_flip_exit(bars_3m, held, flip_pos, now=now,
                                      h50_hold_seen=h50_hold_seen,
                                      etf_move_pct=etf_move_pct,
                                      already_armed=flip_exit_armed)
        if exit_dec.exit_now:
            reasons.append(exit_dec.reason)
            return X1Context(final_action=ACTION_EXIT, flip=flip_all,
                             flip_exit=exit_dec, context_score=exit_dec.score,
                             reasons=tuple(reasons))

    morning = X1MorningContext()
    if d is not None and is_morning:
        morning = evaluate_morning_context(bars_3m, bars_1m, d, now=now)
        reasons.append(morning.reason)

    bonus, bonus_metrics = (False, {})
    if d is not None:
        bonus, bonus_metrics = afternoon_blue_bonus(bars_3m, d, now=now)
    if bonus:
        reasons.append("X1_AFTERNOON_BLUE_BONUS")

    score = morning.score + (config.X1_AFTERNOON_BLUE_BONUS if bonus else 0)

    # 이미 기존 로직이 승인한 신호 -> X1 은 통과/약함만 표시한다.
    if base_approved:
        action = ACTION_PASS
        if is_morning and morning.action == ACTION_WATCH:
            action = ACTION_WATCH
        elif is_morning and morning.action == ACTION_PASS_WEAK:
            action = ACTION_PASS_WEAK
        return X1Context(final_action=action, morning=morning, flip=flip_all,
                         flip_exit=exit_dec, afternoon_blue_bonus=bonus,
                         context_score=score, reasons=tuple(reasons))

    # 판정할 대상 자체가 없으면(신호 없음/거절사유 없음) 아무 말도 하지 않는다.
    # fail-open — X1 이 "정보 없음"을 BLOCK 으로 바꾸는 일은 없다.
    if not base_reject_reason:
        reasons.append("X1_NO_BASE_DECISION")
        return X1Context(final_action=ACTION_PASS, morning=morning, flip=flip_all,
                         flip_exit=exit_dec, afternoon_blue_bonus=bonus,
                         context_score=score, reasons=tuple(reasons))

    # 거절된 신호 — hard reject 는 여기서 끝이다.
    if base_reject_reason in HARD_REJECT_REASONS:
        reasons.append("X1_HARD_REJECT_NOT_RESURRECTED")
        return X1Context(final_action=ACTION_BLOCK, morning=morning, flip=flip_all,
                         flip_exit=exit_dec, afternoon_blue_bonus=bonus,
                         context_score=score, reasons=tuple(reasons))

    reentry = evaluate_afternoon_reentry(teg_decision, base_reject_reason=base_reject_reason)
    if reentry.allowed:
        reasons.append(reentry.reason)
        return X1Context(final_action=ACTION_REENTRY, morning=morning, flip=flip_all,
                         flip_exit=exit_dec, reentry=reentry,
                         afternoon_blue_bonus=bonus, context_score=score,
                         reasons=tuple(reasons))

    watch = evaluate_flip_watch(flag_events, now=now, bars_3m=bars_3m)
    late = evaluate_late_entry(bars_3m, watch, now=now,
                               base_reject_reason=base_reject_reason,
                               etf_move_pct=etf_move_pct)
    reasons.append(late.reason)
    if late.candidate and late.within_guard:
        return X1Context(final_action=ACTION_LATE_ENTRY, morning=morning, flip=watch,
                         flip_exit=exit_dec, reentry=reentry, late_entry=late,
                         afternoon_blue_bonus=bonus, context_score=score,
                         reasons=tuple(reasons))
    if watch.watching:
        return X1Context(final_action=ACTION_WATCH, morning=morning, flip=watch,
                         flip_exit=exit_dec, reentry=reentry, late_entry=late,
                         afternoon_blue_bonus=bonus, context_score=score,
                         reasons=tuple(reasons))
    return X1Context(final_action=ACTION_BLOCK, morning=morning, flip=watch,
                     flip_exit=exit_dec, reentry=reentry, late_entry=late,
                     afternoon_blue_bonus=bonus, context_score=score,
                     reasons=tuple(reasons))
