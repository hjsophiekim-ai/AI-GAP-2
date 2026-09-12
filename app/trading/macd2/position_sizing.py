"""W1a 포지션 사이징 — X2-lite 전용, 주문수량만 바꾸는 순수 모듈 (2026-09-12).

이 모듈이 절대 하지 않는 것
---------------------------
진입/청산 판정을 하지 않는다. MACD 플래그 생성, T+3 confirmation, TW TEG,
3-SLOT 슬롯 배분, quality gate, CHOP **판정 자체**, same/opposite 규칙,
신규진입 cutoff, 하루 3회 cap, TP/SL/ETP/trailing/반대신호/강제청산은 이
파일에서 import 조차 하지 않는 영역이고 한 줄도 수정되지 않았다. 여기 있는
함수는 **이미 승인된 진입의 주문수량 배수**만 답한다. 거래를 추가하거나 삭제할
수 있는 반환값이 아예 존재하지 않는다.

W1a 규칙 (config.X2LITE_SIZING_* 참조)
--------------------------------------
    기본                                   1.00
    진입 확정봉이 CHOP                       x0.80
    그날 첫 거래가 STOP_LOSS 로 종료된 뒤       x1.20
    둘 다                                  0.96
    clip                                  0.25 ~ 1.50
    일일 누적 exposure                      최대 3.00 (greedy)

"그날 첫 거래가 STOP_LOSS" 의 정의
----------------------------------
그날 **첫 번째 진입**이 ``config.EXIT_TW_STOP_LOSS`` 로 **전량청산**된 경우만
참이다. 아래는 전부 거짓으로 둔다 — 연구 하네스와 같은 정의다:
  - TP1 이후 잔량 stop (``EXIT_TW_AFTER_TP1_STOP``)
  - trailing stop / breakeven / profit-lock stop
  - 반대신호 청산 / whipsaw 청산 / 강제청산 / 조기익절
  - 부분익절(TP1)만 일어난 상태, 평가손실
날짜가 바뀌면 리셋된다. 상태는 디스크에 저장돼 재시작 후에도 복원된다.

exposure 는 **진입 시점 누적**이다 — 부분익절이나 청산이 누적을 되돌리지
않는다(연구 사양 그대로). greedy 이므로 앞선 진입을 소급해 줄이지 않고, 남은
한도가 모자라면 그 진입의 수량만 깎인다.

순수 함수만 있다(``risk_exit.py``/``time_window_position_manager.py`` 와 같은
계약): 네트워크/브로커 접근 없음. ``note_*`` 함수만 state 를 갱신한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.trading.macd2 import config
from app.trading.macd2 import time_window_3slot


@dataclass(frozen=True)
class SizingDecision:
    """``applied`` 가 실제 주문에 곱해질 배수다."""
    active: bool          # W1a 가 이 진입에 적용되는가
    raw: float            # clip 전 규칙 배수
    clipped: float        # 25~150% clip 후
    applied: float        # 일일 exposure 상한까지 적용한 최종 배수
    exposure_before: float
    exposure_after: float
    capped: bool          # 일일 상한 때문에 깎였는가
    reason: str           # 진단용 라벨


NEUTRAL = SizingDecision(
    active=False, raw=1.0, clipped=1.0, applied=1.0,
    exposure_before=0.0, exposure_after=0.0, capped=False, reason="NOT_ACTIVE",
)


def is_active(state) -> bool:
    """W1a 가 적용되는 상태인가 — X2-lite 모드일 때만 True.

    다른 모든 전략(F / TW TEG 3-SLOT / TW2 3-SLOT / TW2 / TEGv2 / 무필터 /
    MU_MACD)에서는 False 이므로 배수가 1.0 으로 고정돼 동작이 조금도 바뀌지
    않는다. 토글(``time_window_x2lite_filter_enabled``)을 보는 이유는, 진입이
    체결되기 **전에** 호출되기 때문이다 — 그 시점에는
    ``state.time_window_active_mode`` 가 아직 이번 포지션 값으로 갱신되지
    않았다."""
    if not bool(getattr(config, "X2LITE_SIZING_ENABLED", True)):
        return False
    return (time_window_3slot.active_3slot_mode(state)
            == time_window_3slot.MODE_X2LITE_3SLOT)


def first_trade_stop_loss_active(state) -> bool:
    return bool(getattr(state, "x2lite_first_trade_stop_loss", False))


def exposure_used(state) -> float:
    return float(getattr(state, "x2lite_exposure_used_today", 0.0) or 0.0)


def raw_multiplier(state, *, entry_chop: bool) -> tuple[float, str]:
    """clip/cap 전 규칙 배수와 진단 라벨."""
    w = 1.0
    parts = []
    if bool(entry_chop):
        w *= float(config.X2LITE_SIZING_CHOP_MULT)
        parts.append("CHOP")
    if first_trade_stop_loss_active(state):
        w *= float(config.X2LITE_SIZING_POST_STOP_MULT)
        parts.append("POST_STOP")
    return w, ("+".join(parts) if parts else "BASE")


def evaluate(state, *, entry_chop: bool) -> SizingDecision:
    """이 진입에 적용할 배수. state 를 갱신하지 않는다(순수)."""
    if not is_active(state):
        return NEUTRAL
    raw, label = raw_multiplier(state, entry_chop=entry_chop)
    lo = float(config.X2LITE_SIZING_MIN_MULT)
    hi = float(config.X2LITE_SIZING_MAX_MULT)
    clipped = max(lo, min(hi, raw))
    used = exposure_used(state)
    room = float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP) - used
    if room <= 0:
        applied, capped = 0.0, True
    elif clipped > room:
        applied, capped = room, True
    else:
        applied, capped = clipped, False
    return SizingDecision(
        active=True, raw=raw, clipped=clipped, applied=applied,
        exposure_before=used, exposure_after=used + applied,
        capped=capped, reason=(label + ("+CAPPED" if capped else "")),
    )


def note_entry(state, decision: SizingDecision) -> None:
    """진입이 **체결된 뒤** 호출 — 누적 exposure 와 진입 순번을 올린다."""
    if not decision.active:
        return
    state.x2lite_exposure_used_today = round(
        exposure_used(state) + float(decision.applied), 6)
    state.x2lite_entry_seq_today = int(getattr(state, "x2lite_entry_seq_today", 0) or 0) + 1
    state.x2lite_last_applied_sizing = float(decision.applied)


def note_full_exit(state, exit_reason: Optional[str]) -> None:
    """전량청산이 **확정된 뒤** 호출 — 그날 첫 거래가 STOP_LOSS 였는지 기록.

    호출 시점에 ``x2lite_entry_seq_today == 1`` 이면 지금 닫힌 포지션이 그날
    첫 거래다(한 번에 한 포지션만 보유하므로, 두 번째 진입은 첫 거래가 닫힌
    뒤에만 일어난다). 이미 True 면 다시 덮어쓰지 않는다."""
    if not is_active(state):
        return
    if int(getattr(state, "x2lite_entry_seq_today", 0) or 0) != 1:
        return
    if str(exit_reason or "") == config.EXIT_TW_STOP_LOSS:
        state.x2lite_first_trade_stop_loss = True


def reset_daily(state) -> None:
    """날짜 rollover — exposure/순번/첫거래 플래그를 전부 초기화한다.
    토글 자체(``time_window_x2lite_filter_enabled``)는 건드리지 않는다."""
    state.x2lite_exposure_used_today = 0.0
    state.x2lite_entry_seq_today = 0
    state.x2lite_first_trade_stop_loss = False
    state.x2lite_last_applied_sizing = None


def describe(state) -> str:
    """UI/로그용 한 줄 요약."""
    if not is_active(state):
        return "OFF"
    return (f"{config.X2LITE_SIZING_NAME} "
            f"CHOP {config.X2LITE_SIZING_CHOP_MULT * 100:.0f}% / "
            f"first-stop 이후 {config.X2LITE_SIZING_POST_STOP_MULT * 100:.0f}% · "
            f"오늘 exposure {exposure_used(state) * 100:.0f}%/"
            f"{config.X2LITE_SIZING_DAILY_EXPOSURE_CAP * 100:.0f}%"
            + (" · 첫거래 손절" if first_trade_stop_loss_active(state) else ""))
