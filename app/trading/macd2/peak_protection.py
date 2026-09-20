"""C1 — Peak Protection, N1 계열 전용 독립 overlay (2026-09-19).

이 모듈이 절대 하지 않는 것
---------------------------
진입 판정을 하지 않는다. MACD 플래그 생성, T+3 confirmation, TW TEG,
3-SLOT 슬롯 배분, quality gate, CHOP 판정, TEGv2, 신규진입 cutoff,
하루 3회 cap, W1a sizing, 주문 수량 계산은 이 파일에서 import 조차 하지
않는 영역이고 한 줄도 수정되지 않았다. **C1 은 진입집합을 바꾸지 않는다.**

TP1 / TP2 / off_tp2(8%↔4%) 적응 / 손절 / after-TP1 스탑 / trailing /
breakeven / profit-lock / 조기익절(ETP) / 강제청산도 건드리지 않는다.
C1 은 **그 래더가 전부 HOLD 라고 답한 뒤에만** 발언한다 —
``small_whipsaw_hold``(H50) 가 worker 안에서 차지하는 자리와 같은 계약이다.

이 모듈이 하는 것 — 단 하나
---------------------------
보유 포지션이 **MFE(틱 관측 최고 순수익률) >= C1_ARM_MFE_PCT** 에 도달한 뒤,
**완성 3분봉**에서 아래 두 조건이 **동시에** 성립하면 잔량을 전량청산한다.

    (a) MACD-Signal gap 이 보유방향 **반대로 전환**
            UP_RED   보유 -> gap <= 0
            DOWN_BLUE 보유 -> gap >= 0
    (b) MFE 대비 **>= C1_GIVEBACK_PCT** 반납
            net_return_pct <= peak - C1_GIVEBACK_PCT

(a) 는 **단순 gap 축소가 아니라 부호 전환**이다. 2026-09-19 연구에서
단순 축소(gap_shrink)는 무조건 발동과 구별되지 않았고 TP2 8% runner 를 깼다
(78일 +4.47 / runner -3.80), 부호 전환만 78일 +37.54 / runner 손상 0 이었다.

gap 은 ``signal_engine.calculate_macd`` 가 이미 계산해 둔
``MacdSnapshot.hist`` (= macd - signal) 을 그대로 쓴다. **새 MACD 계산식을
만들지 않는다** — worker 가 매 tick 넘겨주는 바로 그 스냅샷이다.

왜 "완성봉"인가
---------------
하방 rung 은 전부 완성봉 종가 기준이라는 기존 설계(노이즈 틱 하나로 스탑을
때리지 않는다)를 그대로 따른다. arm 판정의 MFE 만 틱 관측값인데, 이는
``early_take_profit`` 의 ``early_tp_peak_net_return`` 과 동일한 계약이다
(production 의 ``time_window_peak_net_return`` 은 완성봉 전용이라 건드리지
않는다).

순수 함수만 있다(``risk_exit.py`` / ``small_whipsaw_hold.py`` 와 같은 계약):
네트워크/브로커 접근 없음. ``note_*`` / ``clear`` 만 state 를 갱신한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.trading.macd2 import config
from app.trading.macd2 import time_window_3slot
from app.trading.macd2.models import Direction

#: 이 모듈이 내는 유일한 청산 사유.
EXIT_C1_PEAK_PROTECTION = "C1_PEAK_PROTECTION_EXIT"

LABEL_DISABLED = "DISABLED"
LABEL_NOT_ARMED = "NOT_ARMED"
LABEL_NO_GAP_REVERSAL = "NO_GAP_REVERSAL"
LABEL_GIVEBACK_TOO_SMALL = "GIVEBACK_TOO_SMALL"
LABEL_FIRED = "FIRED"


@dataclass(frozen=True)
class PeakProtectionDecision:
    """C1 판정 결과. ``exit_reason`` 이 None 이면 HOLD."""

    armed: bool                      # MFE >= arm 에 도달했는가
    gap_reversed: bool               # gap 이 보유방향 반대로 전환됐는가
    giveback_pct: float              # peak - net (음수면 신고점 갱신 중)
    exit_reason: Optional[str]       # EXIT_C1_PEAK_PROTECTION 또는 None
    sell_fraction: float             # 발동 시 1.0 (전량), 아니면 0.0
    label: str                       # 진단용 단계 라벨


def is_enabled(state) -> bool:
    """토글이 켜져 있는가 (모드 조건은 보지 않는다)."""
    if not bool(getattr(config, "C1_ENABLED", False)):
        return False
    return bool(getattr(state, "c1_peak_protection_enabled", False))


def is_supported_mode(state) -> bool:
    """C1 을 적용할 수 있는 전략인가.

    C1 은 TP2 가 8% 까지 열리는 N1 계열 래더를 전제로 만들어졌다 — peak 5%
    도달 후 TP2 사이의 무보호 구간을 메우는 규칙이기 때문이다. TP2 가 5.0%
    인 X2-lite 계열에서는 peak 5% 도달 즉시 TP2 가 전량청산하므로 C1 은
    구조적으로 발동할 일이 없다(2026-09-19 연구에서 이식 시 발동 0건 확인).
    그래도 "켤 수 있는 모드"를 X2-lite 계열로 제한해 두는 이유는, N1 래더가
    production 에 들어오면 그 모드가 이 계열 위에 얹히기 때문이다.
    """
    return time_window_3slot.active_3slot_mode(state) in time_window_3slot.MODES_X2LITE_FAMILY


def is_active(state) -> bool:
    """이 tick 에서 C1 이 실제로 평가되는가. 기본값은 항상 False."""
    return is_enabled(state) and is_supported_mode(state)


def thresholds(state=None) -> tuple[float, float]:
    """(arm MFE %, giveback %p). state 는 장래 모드별 분기를 위한 자리다."""
    return (float(config.C1_ARM_MFE_PCT), float(config.C1_GIVEBACK_PCT))


def gap_reversed(held_direction: Optional[Direction], macd_hist: Optional[float]) -> bool:
    """보유방향 기준 MACD-Signal gap 이 **반대로 전환**됐는가.

    ``macd_hist`` 는 ``MacdSnapshot.hist`` (= macd - signal) 그대로.
    단순 축소는 여기서 True 가 되지 않는다 — 부호가 넘어가야 한다.
    """
    if held_direction is None or macd_hist is None:
        return False
    try:
        value = float(macd_hist)
    except (TypeError, ValueError):
        return False
    if held_direction == Direction.UP_RED:
        return value <= 0.0
    if held_direction == Direction.DOWN_BLUE:
        return value >= 0.0
    return False


def evaluate(
    *,
    held_direction: Optional[Direction],
    peak_net_return_pct: float,
    net_return_pct: float,
    macd_hist: Optional[float],
    arm_pct: Optional[float] = None,
    giveback_pct: Optional[float] = None,
) -> PeakProtectionDecision:
    """``peak_net_return_pct`` = 진입 후 MFE(틱 관측 최고 순수익률, %),
    ``net_return_pct`` = 판정 대상 **완성봉 종가** 기준 순수익률(%).

    호출자는 production 래더가 아무 청산도 내지 않았을 때만 이 함수를 부른다 —
    즉 여기서 나오는 exit_reason 은 절대 TP1/TP2/손절/trailing/강제청산을
    앞지르지 않는다."""
    arm = float(config.C1_ARM_MFE_PCT) if arm_pct is None else float(arm_pct)
    give = float(config.C1_GIVEBACK_PCT) if giveback_pct is None else float(giveback_pct)

    peak = float(peak_net_return_pct)
    net = float(net_return_pct)
    drop = peak - net

    armed = peak >= arm
    if not armed:
        return PeakProtectionDecision(False, False, drop, None, 0.0, LABEL_NOT_ARMED)

    reversed_ = gap_reversed(held_direction, macd_hist)
    if not reversed_:
        return PeakProtectionDecision(True, False, drop, None, 0.0, LABEL_NO_GAP_REVERSAL)
    if drop < give:
        return PeakProtectionDecision(True, True, drop, None, 0.0, LABEL_GIVEBACK_TOO_SMALL)
    return PeakProtectionDecision(
        True, True, drop, EXIT_C1_PEAK_PROTECTION, 1.0, LABEL_FIRED,
    )


# ── state 헬퍼 (독립 namespace, 기존 N1/H50/whipsaw state 와 섞지 않는다) ──
def note_peak(state, net_return_pct: float) -> None:
    """틱 관측 MFE 갱신. C1 이 active 일 때만 호출된다."""
    state.c1_peak_net_return = max(
        float(getattr(state, "c1_peak_net_return", 0.0) or 0.0), float(net_return_pct),
    )


def note_armed(state, when) -> None:
    if not getattr(state, "c1_armed", False):
        state.c1_armed = True
        state.c1_armed_at = (
            when.isoformat() if hasattr(when, "isoformat") else (when or None)
        )


def note_checked_bar(state, bar_ts) -> None:
    state.c1_last_checked_bar_ts = (
        bar_ts.isoformat() if hasattr(bar_ts, "isoformat") else (bar_ts or None)
    )


def note_triggered(state, when) -> None:
    state.c1_triggered_at = (
        when.isoformat() if hasattr(when, "isoformat") else (when or None)
    )


def clear(state) -> None:
    """포지션 종료 / 일자변경 / 재시작 정리 공통 경로.

    토글(``c1_peak_protection_enabled``)은 여기서 건드리지 않는다 — 그것은
    사용자 설정이고, 이 함수는 **보유기간 상태**만 되돌린다."""
    state.c1_armed = False
    state.c1_armed_at = None
    state.c1_peak_net_return = 0.0
    state.c1_last_checked_bar_ts = None
    state.c1_triggered_at = None


def is_armed(state) -> bool:
    return bool(getattr(state, "c1_armed", False))
