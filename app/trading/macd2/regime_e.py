"""REGIME_E — E 돌파대기 모드(E / CHOP)의 **TREND 각인 진입만** E 돌파대기를 생략한다.

2026-11-02 도입(기본 OFF) → 2026-11-03 부터 **토글 없는 기본 동작**이다.
바뀌는 것은 **E 돌파대기를 걸지 말지 하나뿐**이다. 진입 승인은 이 판정보다 **앞에서 이미
끝나 있다** -- T+3 / TEG / VWAP veto / MU Assist(ENTRY_BAD veto · CATCH-UP) / XS / 슬롯·하루한도 /
사이징은 한 줄도 보지 않고 바꾸지 않는다. 청산(손절/TP/C1/H50/B3)도 마찬가지다. regime 판정
로직(:mod:`chop_regime`)도 읽기만 한다.

발동 조건 (전부 만족해야 bypass)
  1. ``strategy_mode.current(state)`` 가 E 돌파대기 모드(E / CHOP) -- N1/P3 는 E 대기 자체가 없다
  2. 그 tick 의 ``state.p3_last_regime == "TREND"``  -- 이 값이 곧 진입에 각인될 regime이다
     (``worker._finalize_tw2_3slot_entry`` 가 같은 필드로 ``p3_entry_regime`` 을 찍는다)
  3. 원래 E 돌파대기 대상이었을 것 (``early.immediate`` 가 False 이고 trigger 가 있을 것)

bypass 하면 호출부는 대기를 걸지 않고 **T+3 승인 즉시진입 경로**로 그대로 내려간다 --
``_e_arm_pending_instead_of_entry`` 가 ``early.trigger is None`` 일 때 False 를 돌려주는 것과
**같은 경로**다. 그래서 bypass 는 새 진입경로를 만들지 않는다.

CHOP/WARMUP 각인 진입은 조건 2에서 떨어져 기존 E 돌파대기가 그대로 유지된다.

근거: ``research_20261102_reclaim/REPORT_REGIME_E.md`` (CHOP 58일) ·
``REPORT_REGIME_E_MU.md`` (MU Assist 켠 11일).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.trading.macd2 import chop_regime, config, strategy_mode

#: bypass 사유 — 원장/로그에 그대로 적힌다. 모드별로 구분한다.
BYPASS_REASON = "CHOP_MODE_TREND_REGIME"
BYPASS_REASON_E = "E_MODE_TREND_REGIME"

#: bypass 하지 않은 이유들 (진단용, 원장에 그대로 적힌다).
SKIP_NOT_E_WAIT_MODE = "NOT_E_WAIT_MODE"
#: 이전 이름 호환 (2026-11-02 은 CHOP 모드 전용이었다).
SKIP_NOT_CHOP_MODE = SKIP_NOT_E_WAIT_MODE
SKIP_NOT_TREND_REGIME = "NOT_TREND_REGIME"
SKIP_NOT_E_WAIT = "NOT_E_WAIT_CANDIDATE"


@dataclass(frozen=True)
class RegimeEDecision:
    """한 T+3 후보에 대한 판정. ``bypassed`` 외에는 전부 기록용이다."""

    bypassed: bool
    reason: str
    regime: str
    strategy_mode: str
    wait_eligible: bool
    t3_price: Optional[float]
    e_trigger: Optional[float]

    @property
    def price_edge_pct(self) -> Optional[float]:
        """E 돌파가 요구했을 가격이 T+3 기준가보다 얼마나 불리한가(%).

        + 면 E 로 갔다면 그만큼 비싸게(UP) / 유리하지 않게 샀을 것이라는 뜻이다.
        둘 중 하나라도 없으면 ``None``. **판정에는 쓰지 않는다 -- 기록 전용이다.**
        """
        if not self.t3_price or not self.e_trigger or self.t3_price <= 0:
            return None
        return round((float(self.e_trigger) / float(self.t3_price) - 1.0) * 100.0, 4)


def is_enabled(state: Any = None) -> bool:
    """항상 True — 2026-11-03 부터 토글이 없다(원장 ``regime_e_enabled`` 컬럼 호환용)."""
    return True


def applies_to_mode(mode: str) -> bool:
    """E 돌파대기가 있는 모드(E / CHOP)인가."""
    return mode in strategy_mode.E_BASED_MODES


def current_regime(state: Any) -> str:
    """그 tick 의 regime 각인값. 비어 있으면 WARMUP -- ``_finalize_tw2_3slot_entry`` 와 같은 정의."""
    return str(getattr(state, "p3_last_regime", None) or chop_regime.REGIME_WARMUP)


def evaluate(state: Any, *, early: Any, price: Optional[float]) -> RegimeEDecision:
    """이 T+3 후보의 E 돌파대기를 생략할 것인가.

    ``early`` 는 :func:`e_strategy.evaluate_early_pass` 결과다. 이 함수는 state 를
    **읽기만 한다** -- 주문도, 슬롯도, 예산도 건드리지 않는다.
    """
    regime = current_regime(state)
    mode = strategy_mode.current(state)
    wait_eligible = bool(early is not None and not getattr(early, "immediate", False)
                         and getattr(early, "trigger", None) is not None)
    trigger = getattr(early, "trigger", None) if early is not None else None
    base = dict(regime=regime, strategy_mode=mode, wait_eligible=wait_eligible,
                t3_price=(float(price) if price else None),
                e_trigger=(float(trigger) if trigger is not None else None))
    if not applies_to_mode(mode):
        return RegimeEDecision(bypassed=False, reason=SKIP_NOT_E_WAIT_MODE, **base)
    if regime != chop_regime.REGIME_TREND:
        return RegimeEDecision(bypassed=False, reason=SKIP_NOT_TREND_REGIME, **base)
    if not wait_eligible:
        return RegimeEDecision(bypassed=False, reason=SKIP_NOT_E_WAIT, **base)
    reason = BYPASS_REASON if mode == strategy_mode.MODE_CHOP else BYPASS_REASON_E
    return RegimeEDecision(bypassed=True, reason=reason, **base)


def trace_fields(dec: Optional[RegimeEDecision]) -> dict[str, Any]:
    """``result.signal_dispatch_trace["regime_e"]`` 에 넣을 dict."""
    if dec is None:
        return {}
    return {
        "bypass": bool(dec.bypassed),
        "reason": dec.reason,
        "regime": dec.regime,
        "strategy_mode": dec.strategy_mode,
        "wait_eligible": bool(dec.wait_eligible),
        "t3_price": dec.t3_price,
        "e_trigger": dec.e_trigger,
        "price_edge_pct": dec.price_edge_pct,
    }


def ledger_fields(trace: Optional[dict]) -> dict[str, Any]:
    """신호원장 컬럼 — 이 진입이 REGIME_E 로 E 를 생략한 건인지, 아니면 왜 아닌지.

    ``trace`` 가 없으면 빈 dict 라 기존 행 모양이 조금도 바뀌지 않는다.
    """
    if not trace:
        return {}
    edge = trace.get("price_edge_pct")
    return {
        "regime_e_enabled": True,
        "regime_e_version": config.REGIME_E_VERSION,
        "e_bypass": bool(trace.get("bypass")),
        "e_bypass_reason": str(trace.get("reason") or ""),
        "regime_e_regime": str(trace.get("regime") or ""),
        "regime_e_strategy_mode": str(trace.get("strategy_mode") or ""),
        "e_wait_eligible": bool(trace.get("wait_eligible")),
        "e_bypass_t3_price": "" if trace.get("t3_price") is None else trace["t3_price"],
        "e_bypass_e_trigger": "" if trace.get("e_trigger") is None else trace["e_trigger"],
        "e_bypass_price_edge_pct": "" if edge is None else edge,
    }


def describe(state: Any) -> str:
    """UI 한 줄 요약."""
    return (f"REGIME_E ON · mode={strategy_mode.current(state)} · "
            f"regime={current_regime(state)}")
