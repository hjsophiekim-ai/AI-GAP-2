"""B3 / P3 / Y3 — CHOP 진입 포지션 전용 청산 스택 (2026-09-27).

**순수 판정 모듈이다.** 주문을 내지 않고 state 를 바꾸지 않는다 -- worker 가
결과를 받아 기존 order_executor 경로로 집행한다(peak_protection.py 와 같은 관례).

세 규칙은 전부 ``entry_regime == CHOP`` 으로 진입한 포지션에만 적용된다.
TREND / WARMUP 으로 진입한 포지션은 이 모듈을 단 한 번도 통과하지 않는다.

    B3  틱 기준 TP +1.0% / SL -1.0% / max-hold 20분.
        **틱 익절 래더만** 대체한다 -- 완성봉 손절/after-TP1-stop/trailing,
        OPPOSITE_SIGNAL, whipsaw, H50, C1, 강제청산은 그대로 살아 있다.
        (연구엔진 hengine5 의 exit mode="replace" 와 동일한 범위다.)

    P3  진입 -> **최초** +1.0% 도달 경과시간이 6분 이하면 일부 익절 +
        잔량 승격. gap/ETF/spread 같은 현재 모멘텀을 보지 않는다 -- 경로형태
        단일조건이다(2026-09-25 연구 P3_fast6).
        Q2(2026-09-28): 그 익절비중이 50% -> 20% 다. 조건은 그대로다.

    Y3  max-hold 20분 시점에 net > 0 ∧ MACD gap 이 보유방향으로 확대 ∧
        보유 ETF 추종이면 청산 취소 + 승격. 셋 중 하나라도 거짓이면 청산.

    H30 (2026-09-28) max-hold 20분 시점에 **H50 HOLD 가 이미 활성**이면 즉시
        자르지 않고 진입 + 30분까지 유예한다. 유예 중에도 손절/강제청산/
        세션종료/H50 자신의 해제규칙은 전부 먼저다 -- H30 이 늦추는 것은
        **B3 max-hold 하나뿐**이다. 30분에 다시 Y3 를 판정해 승격 아니면 청산
        (``P3_H30_MAXHOLD_EXIT``). 유예 중 +1% 최초 도달이면 Q2 와 같은 비중으로
        부분익절 + 승격한다. H50 이 비활성이면 이 규칙은 한 줄도 타지 않는다.

승격(promoted)된 포지션은 그 즉시 B3 관리에서 빠지고 기존 N1/C1 래더로
돌아간다 -- 이후 TP1/TP2/오후TP/trailing/C1 이 정상 동작한다.

미래참조 차단: gap 판정은 **마지막 완성봉**만 본다. 엔진 틱은
``bar_start + 0/1/2분`` 에 돌지만 그 봉은 ``bar_start + 3분`` 에야 완성되므로,
형성 중인 봉을 보면 미래를 보는 것이다(:func:`last_completed_bar_index`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from app.trading.macd2 import config
from app.trading.macd2.models import Direction

ACTION_HOLD = "HOLD"
ACTION_EXIT = "EXIT"
ACTION_PARTIAL_PROMOTE = "PARTIAL_PROMOTE"
ACTION_PROMOTE = "PROMOTE"
#: H30 — max-hold 청산을 유예한다. 주문이 나가지 않는 유일한 비-HOLD 액션이다.
ACTION_EXTEND = "EXTEND"

MODE_BASE = "BASE"
MODE_B3 = "B3"
MODE_H30 = "H30"
MODE_P3_RUNNER = "P3-RUNNER"
MODE_Y3_RUNNER = "Y3-RUNNER"


@dataclass
class B3Decision:
    """B3/P3/Y3 한 번의 판정. worker 가 이것만 보고 집행한다."""

    action: str = ACTION_HOLD
    exit_reason: Optional[str] = None
    sell_fraction: float = 0.0
    promote: bool = False
    rescue: bool = False
    reason: str = ""
    elapsed_min: Optional[float] = None
    net_pct: Optional[float] = None
    conditions: str = ""


def is_enabled(state) -> bool:
    """P3 가 이 상태에서 켜져 있는가 (토글 + 전역 kill-switch)."""
    if not bool(getattr(config, "P3_ENABLED", True)):
        return False
    return bool(getattr(state, "p3_enabled", False))


def is_supported_mode(state) -> bool:
    """P3 를 쓸 수 있는 전략 모드인가 -- **N1 계열 전용**이다.

    연구 BASE 는 N1 + C1 (+ SMART) 하나뿐이다. X2-lite W1 / H50 / TW2 3-SLOT
    에서는 B3/Y3/P3 가 한 번도 검증된 적이 없으므로 여기서 잘라낸다. C1 이
    같은 이유로 N1 계열에서만 켜지는 것과 정확히 같은 관례다.
    """
    # 지연 import -- time_window_3slot 이 이 모듈을 import 하지는 않지만,
    # 모듈 간 import 그래프를 단방향으로 유지한다.
    from app.trading.macd2 import time_window_3slot

    return time_window_3slot.active_3slot_mode(state) in time_window_3slot.MODES_N1_FAMILY


def is_active(state) -> bool:
    """지금 tick 에서 P3 스택을 평가해도 되는가."""
    return is_enabled(state) and is_supported_mode(state)


# ── 미래참조 차단 ─────────────────────────────────────────────────────────
def last_completed_bar_index(bars_3m, now: datetime) -> Optional[int]:
    """``now`` 시점에 **이미 완성된** 마지막 3분봉의 위치(iloc).

    3분봉 ``i`` 의 라벨은 ``bar_start`` 이고 실제 완성은 ``bar_start + 3분``
    이다. 따라서 ``bar_start + 3분 <= now`` 인 마지막 봉만 쓸 수 있다.
    형성 중인 봉을 쓰면 그 봉의 종가는 미래값이다.

    ``None`` 이면 쓸 수 있는 완성봉이 없다는 뜻이고, 호출부는 조건 불충족으로
    처리한다(추정하지 않는다).
    """
    if bars_3m is None or len(bars_3m) == 0:
        return None
    try:
        stamps = bars_3m["datetime"]
    except Exception:
        return None
    limit = now - timedelta(minutes=3)
    idx: Optional[int] = None
    for i in range(len(stamps) - 1, -1, -1):
        ts = stamps.iloc[i]
        start = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if start is None:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=config.KST)
        if start <= limit:
            idx = i
            break
    return idx


def macd_hist_series(bars_3m) -> Optional[list[float]]:
    """MACD 히스토그램(= MACD - signal) 시계열.

    연구 배터리와 **같은 식**이다: EMA12 - EMA26 을 MACD 로 보고 그 EMA9 를
    signal 로 빼서 히스토그램을 만든다. Y3 의 "gap 확대" 는 이 히스토그램의
    봉간 변화 부호로 판정한다.
    """
    if bars_3m is None or len(bars_3m) < 2:
        return None
    try:
        close = bars_3m["close"].astype(float)
    except Exception:
        return None
    fast = int(config.P3_Y3_MACD_FAST)
    slow = int(config.P3_Y3_MACD_SLOW)
    sig = int(config.P3_Y3_MACD_SIGNAL)
    macd = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    hist = macd - macd.ewm(span=sig, adjust=False).mean()
    return [float(v) for v in hist.tolist()]


def _direction_sign(direction: Any) -> float:
    """레버리지 보유(+1) / 인버스 보유(-1).

    Y3 의 gap 조건은 "보유방향으로 확대" 이므로 부호를 맞춰야 한다.
    """
    value = getattr(direction, "value", None) or str(direction or "")
    return 1.0 if value == Direction.UP_RED.value else -1.0


def macd_gap_expanding(bars_3m, now: datetime, direction: Any) -> tuple[bool, str]:
    """마지막 완성봉에서 MACD 히스토그램이 보유방향으로 확대됐는가.

    같은 날 직전 완성봉과 비교한다 -- 날짜가 바뀌면 비교하지 않는다(전일
    마지막 봉과 당일 첫 봉의 차이는 의미가 없다).
    """
    idx = last_completed_bar_index(bars_3m, now)
    if idx is None or idx < 1:
        return False, "no_completed_bar"
    hist = macd_hist_series(bars_3m)
    if hist is None or idx >= len(hist):
        return False, "no_hist"
    try:
        cur_ts = bars_3m["datetime"].iloc[idx]
        prev_ts = bars_3m["datetime"].iloc[idx - 1]
    except Exception:
        return False, "no_ts"
    if getattr(cur_ts, "date", None) and getattr(prev_ts, "date", None):
        if cur_ts.date() != prev_ts.date():
            return False, "day_boundary"
    delta = hist[idx] - hist[idx - 1]
    ok = bool(_direction_sign(direction) * delta > 0)
    return ok, f"dgap={delta:+.6f}"


def etf_following(prev_price: Optional[float], current_price: Optional[float]) -> tuple[bool, str]:
    """보유 ETF 가 추종 중인가 -- 3분 전 가격 대비 오르거나 같으면 True.

    **보유 심볼 자신의 가격**으로 본다. 인버스를 들고 있으면 인버스가 올라야
    추종이므로 방향 부호를 따로 곱하지 않는다(연구엔진과 동일).
    """
    if prev_price is None or current_price is None:
        return False, "no_price"
    ok = bool(float(current_price) >= float(prev_price))
    return ok, f"etf={float(current_price) - float(prev_price):+.2f}"


# ── B3 / P3 / Y3 ──────────────────────────────────────────────────────────
def evaluate(
    *,
    net_return_pct: float,
    entry_at: datetime,
    now: datetime,
    direction: Any,
    already_rescued: bool,
    already_promoted: bool,
    bars_3m=None,
    etf_prev_price: Optional[float] = None,
    etf_current_price: Optional[float] = None,
    allow_max_hold: bool = True,
    h50_active: bool = False,
    h30_active: bool = False,
) -> B3Decision:
    """CHOP 포지션 한 건에 대한 B3/P3/Y3 판정.

    ``already_promoted`` 가 True 면 이 포지션은 이미 기존 래더로 돌아간
    상태이므로 아무 것도 하지 않는다(HOLD) -- worker 는 그 경우 이 함수를
    호출하지 않는 것이 정상이지만, 중복호출에도 안전하도록 여기서도 막는다.

    판정 우선순위는 연구엔진과 동일하다: TP -> SL -> max-hold.

    ``allow_max_hold`` 는 worker 의 **두 자리**를 구분한다. TP/SL 은 틱 즉시
    판정이라 ``bars_3m`` 이 아직 없는 이른 청산 체인에서 돌지만, max-hold 의
    Y3 조건은 완성봉과 MACD 히스토그램이 필요해서 늦은 체인(C1/H50 자리)에서
    돈다. 이른 자리에서 ``allow_max_hold=False`` 로 부르지 않으면 봉이 없다는
    이유만으로 Y3 가 항상 거짓이 되어 승격 기회를 통째로 잃는다.

    H30 (2026-09-28)
    ----------------
    ``h50_active`` 는 **지금 H50 HOLD 가 켜져 있는가**, ``h30_active`` 는
    **이 포지션이 이미 연장에 들어갔는가** 다. 둘 다 기본 False 이므로 인자를
    넘기지 않으면 이 함수는 2026-09-27 판정과 한 줄도 다르지 않다.

    연장은 max-hold 도달 시점에 H50 이 켜져 있을 때만 시작되고, 진입 + 30분이
    hard deadline 이다. ``h30_active`` 는 연구엔진의 ``_bs["ext"]`` 와 같은
    계약으로 **한 번 켜지면 포지션이 끝날 때까지 유지된다** -- 연장 중 H50 이
    풀리면 그 tick 에서 곧바로 아래 Y3 판정으로 떨어져 승격/청산으로 끝나므로
    플래그가 남아 돌 여지가 없다.
    """
    if already_promoted:
        return B3Decision(action=ACTION_HOLD, reason="already_promoted",
                          net_pct=float(net_return_pct))

    net = float(net_return_pct)
    elapsed_min = max(0.0, (now - entry_at).total_seconds() / 60.0)
    base = dict(elapsed_min=elapsed_min, net_pct=net)

    # ① TP +1.0% -- 여기서만 P3 rescue 를 본다.
    if net >= float(config.P3_B3_TP_PCT):
        if not already_rescued and elapsed_min <= float(config.P3_RESCUE_MAX_MIN):
            # P3: 일부(Q2 = 20%) 익절 + 잔량 승격. "최초 도달" 판정은 worker 가
            # p3_first_tp_at 을 한 번만 기록하는 것으로 보장한다.
            return B3Decision(
                action=ACTION_PARTIAL_PROMOTE,
                exit_reason=config.EXIT_P3_PARTIAL,
                sell_fraction=float(config.P3_RESCUE_SELL_RATIO),
                promote=True, rescue=True,
                reason="P3_RESCUE",
                conditions=f"fast{elapsed_min:.0f}m",
                **base,
            )
        if not already_rescued and h30_active:
            # H30 연장 중 +1% 최초 도달 -- 6분 창은 이미 지났지만 전량 익절로
            # 끝내지 않고 P3 rescue 와 **같은 비중/같은 구조**로 runner 를
            # 남긴다(연구엔진 H50X_PARTIAL_EXIT). 연장은 여기서 끝나고
            # 잔량은 기존 N1/C1 래더가 맡는다.
            return B3Decision(
                action=ACTION_PARTIAL_PROMOTE,
                exit_reason=config.EXIT_P3_H30_PARTIAL,
                sell_fraction=float(config.P3_RESCUE_SELL_RATIO),
                promote=True, rescue=True,
                reason="P3_H30_RESCUE",
                conditions=f"ext{elapsed_min:.0f}m",
                **base,
            )
        return B3Decision(
            action=ACTION_EXIT, exit_reason=config.EXIT_B3_TP, sell_fraction=1.0,
            reason="B3_TP", conditions=f"slow{elapsed_min:.0f}m", **base,
        )

    # ② SL -1.0%
    if net <= -float(config.P3_B3_SL_PCT):
        return B3Decision(
            action=ACTION_EXIT, exit_reason=config.EXIT_B3_SL, sell_fraction=1.0,
            reason="B3_SL", **base,
        )

    # ③ max-hold 20분 -- 여기서만 Y3 를(그리고 H30 연장을) 본다.
    if allow_max_hold and elapsed_min >= float(config.P3_B3_MAX_HOLD_MIN):
        # ③-a H30: H50 HOLD 가 켜져 있고 아직 30분 전이면 **청산을 미룬다**.
        #     H50 이 꺼져 있으면 이 줄을 그냥 지나가므로 기존 Y3 그대로다.
        #     30분(hard deadline)에 도달하면 H50 이 아직 켜져 있어도 아래로
        #     떨어져 재판정을 받는다 -- "30분까지 무조건 보유" 가 아니다.
        if h50_active and elapsed_min < float(config.P3_H30_EXT_MAX_HOLD_MIN):
            return B3Decision(
                action=ACTION_EXTEND, reason="P3_H30_EXTEND",
                conditions=f"h50_active,deadline{float(config.P3_H30_EXT_MAX_HOLD_MIN):.0f}m",
                **base,
            )
        # ③-b 여기부터는 2026-09-27 Y3 판정 그대로다. 연장을 거친 포지션만
        #     청산사유가 H30 으로 바뀐다(집계에서 구분하기 위함).
        maxhold_reason = (config.EXIT_P3_H30_MAXHOLD if h30_active
                          else config.EXIT_B3_MAXHOLD)
        if net <= 0.0:
            return B3Decision(
                action=ACTION_EXIT, exit_reason=maxhold_reason,
                sell_fraction=1.0, reason="Y3_FAIL_NET", conditions="net<=0", **base,
            )
        gap_ok, gap_tag = macd_gap_expanding(bars_3m, now, direction)
        etf_ok, etf_tag = etf_following(etf_prev_price, etf_current_price)
        conds = ",".join(
            k for k, v in (("gap", gap_ok), ("etf", etf_ok)) if v
        ) or "none"
        if gap_ok and etf_ok:
            return B3Decision(
                action=ACTION_PROMOTE, promote=True, reason="Y3_PROMOTE",
                conditions=f"{conds}|{gap_tag}|{etf_tag}", **base,
            )
        return B3Decision(
            action=ACTION_EXIT, exit_reason=maxhold_reason, sell_fraction=1.0,
            reason="Y3_FAIL_COND", conditions=f"{conds}|{gap_tag}|{etf_tag}", **base,
        )

    return B3Decision(action=ACTION_HOLD, reason="B3_HOLD", **base)


def position_mode(state) -> str:
    """UI/로그가 쓰는 현재 포지션의 관리모드 한 단어."""
    if not bool(getattr(state, "p3_position_active", False)):
        return MODE_BASE
    if bool(getattr(state, "p3_tp_rescued", False)):
        return MODE_P3_RUNNER
    if bool(getattr(state, "y3_promoted", False)):
        return MODE_Y3_RUNNER
    # 승격된 뒤에는 H30 이 아니라 runner 다 -- 위 두 줄이 먼저인 이유.
    if is_h30(state):
        return MODE_H30
    return MODE_B3


# ── state 헬퍼 (독립 namespace — 기존 N1/H50/C1/whipsaw state 와 섞지 않는다) ──
def note_entry_regime(state, regime: str, *, now: Optional[datetime] = None) -> None:
    """신규 포지션이 열릴 때 regime 을 **한 번** 찍는다.

    보유 중 detector 가 바뀌어도 이 값은 소급 변경되지 않는다 -- TREND 로
    들어간 포지션은 끝까지 BASE, CHOP 으로 들어간 포지션은 끝까지 P3 스택이다.
    """
    from app.trading.macd2 import chop_regime

    state.p3_entry_regime = str(regime or chop_regime.REGIME_WARMUP)
    state.p3_position_active = bool(regime == chop_regime.REGIME_CHOP)
    state.p3_first_tp_at = None
    state.p3_tp_rescued = False
    state.p3_tp_rescued_at = None
    state.y3_promoted = False
    state.y3_promoted_at = None
    state.p3_promoted = False
    state.p3_h30_active = False
    state.p3_h30_started_at = None
    state.p3_h30_deadline_at = None


def h30_deadline(entry_at: datetime) -> datetime:
    """이 포지션의 H30 hard deadline = 진입 + 30분.

    **저장값을 믿지 않고 매번 진입시각에서 다시 계산한다.** 재시작 뒤에도
    같은 값이 나오는 유일한 방법이고, 저장된 ``p3_h30_deadline_at`` 은 UI/로그
    표시용 사본일 뿐이다(사용자 요구 §9 "deadline 재계산 오류 없어야 함").
    """
    return entry_at + timedelta(minutes=float(config.P3_H30_EXT_MAX_HOLD_MIN))


def note_h30_start(state, when: datetime, *, entry_at: datetime) -> bool:
    """연장 시작을 **한 번만** 각인한다. True 면 이번이 시작이다.

    이미 연장 중이면 False 를 돌려주므로 호출부가 P3_H30_START 로그를 중복으로
    남기지 않는다(note_first_tp 와 같은 관례).
    """
    if bool(getattr(state, "p3_h30_active", False)):
        return False
    state.p3_h30_active = True
    state.p3_h30_started_at = when.isoformat()
    state.p3_h30_deadline_at = h30_deadline(entry_at).isoformat()
    return True


def is_h30(state) -> bool:
    """이 포지션이 H30 연장을 거쳤는가.

    연구엔진 ``_bs["ext"]`` 와 같이 **한 번 켜지면 포지션이 끝날 때까지** 유지
    된다. 연장 중 H50 이 풀리면 그 tick 의 max-hold 판정이 곧바로 승격/청산으로
    끝내므로 이 플래그가 살아남은 채로 다음 포지션에 새는 일은 없다
    (clear_position 이 진입/청산 양쪽에서 지운다).
    """
    return bool(getattr(state, "p3_h30_active", False))


def note_first_tp(state, when: datetime) -> bool:
    """+1.0% **최초** 도달 시각을 한 번만 기록한다.

    반환값이 True 면 "이번이 최초" 다. 이미 기록돼 있으면 False 이고, 호출부는
    rescue 를 다시 실행하지 않는다 -- 같은 tick 이 두 번 평가돼도 부분매도가
    한 번만 나가는 근거가 이것이다.
    """
    if state.p3_first_tp_at:
        return False
    state.p3_first_tp_at = when.isoformat()
    return True


def note_rescued(state, when: datetime) -> None:
    state.p3_tp_rescued = True
    state.p3_tp_rescued_at = when.isoformat()
    state.p3_promoted = True


def note_y3_promoted(state, when: datetime) -> None:
    state.y3_promoted = True
    state.y3_promoted_at = when.isoformat()
    state.p3_promoted = True


def is_promoted(state) -> bool:
    return bool(getattr(state, "p3_promoted", False))


def governs_position(state) -> bool:
    """이 포지션의 청산을 지금 B3 가 맡고 있는가.

    CHOP 진입 + 아직 미승격일 때만 True 다. 승격됐으면 기존 N1/C1 래더가
    다시 주인이므로 False 가 되고, worker 의 틱 익절 차단도 함께 풀린다.
    """
    return (is_active(state)
            and bool(getattr(state, "p3_position_active", False))
            and not is_promoted(state))


def clear_position(state) -> None:
    """포지션 종료/일자변경/재시작 정리 -- 포지션 스냅샷만 지운다."""
    state.p3_position_active = False
    state.p3_entry_regime = None
    state.p3_first_tp_at = None
    state.p3_tp_rescued = False
    state.p3_tp_rescued_at = None
    state.y3_promoted = False
    state.y3_promoted_at = None
    state.p3_promoted = False
    state.p3_h30_active = False
    state.p3_h30_started_at = None
    state.p3_h30_deadline_at = None


def clear(state) -> None:
    """P3 를 끌 때 -- 포지션 스냅샷 + 섀도우 장부를 함께 정리한다.

    섀도우 **완료거래 ledger 는 지우지 않는다**. 다시 켰을 때 WARMUP 을
    처음부터 다시 쌓지 않아도 되도록 과거 기록은 남겨 둔다.
    """
    clear_position(state)
    state.p3_shadow = None
