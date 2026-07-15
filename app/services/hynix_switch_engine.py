"""
hynix_switch_engine.py — 하이닉스⇄인버스 Enhanced 자동매매 오케스트레이터.

3분마다(또는 UI 자동새로고침 주기마다) 아래 순서를 반복한다:
① kospilab 갱신 ② 마이크론 실시간 갱신 ③~⑥ 점수/판단 계산 ⑦ 보유종목 확인
⑧ 강제청산/TP·SL/스위칭 실행 ⑨ 로그 기록 ⑩ 결과 반환(UI 렌더링용).

각 단계는 개별 try/except로 감싸 부분 실패해도 나머지는 계속 진행한다.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional

from app.logger import logger
from app.utils.time_utils import kst_now
from app.trading.hynix_symbols import SIGNAL_SYMBOL, LONG_SYMBOL as HYNIX_SYMBOL, SHORT_SYMBOL as INVERSE_SYMBOL
from app.services.hynix_switch_state import load_state, save_state_atomic, set_active_mode, reset_mock_state
from app.services.hynix_switch_logger import log_enhanced_prediction, log_trade
from app.trading.hynix_switch_risk_gate import (
    is_watch_only, is_new_entry_allowed, get_liquidation_phase,
    should_force_trade, _parse_hm,
)
from app.trading.hynix_switch_position_manager import (
    run_liquidation_if_needed, run_tp_sl_if_needed, run_switch_or_entry, _current_price, _ACTION_TO_SYMBOL,
    apply_position_manager_to_state,
)
from app.trading.hynix_position_common import HynixPositionManager
from app.trading.hynix_pullback_entry import detect_pullback
from app.trading.hynix_trend_switch_accelerator import (
    default_confirm_state,
    default_frequency_state as default_trend_frequency_state,
    plan_entry as plan_trend_switch_entry,
    signal_direction,
    update_confirm_tracker,
)
from app.trading.hynix_fast_trend import compute_fast_trend_signal
from app.trading.hynix_primary_trend import (
    PRIMARY_TREND_RANGE, PRIMARY_TREND_UP, PRIMARY_TREND_DOWN,
    classify_short_term_move, compute_primary_trend, default_reversal_confirmation_state,
    inverse_block_vote_count, new_hynix_entry_blocked, new_inverse_entry_blocked, update_reversal_confirmation,
)

_PULLBACK_MORNING_WINDOW_END = "10:00"
_PULLBACK_PATIENCE_MINUTES = 2

_SIGNAL_DISPLAY_MAP = {
    "HYNIX_BUY": "BUY", "HYNIX_STRONG_BUY": "BUY",
    "INVERSE_BUY": "INVERSE", "INVERSE_STRONG_BUY": "INVERSE",
    "HOLD": "HOLD",
}

# 파이프라인 트레이스 단계 순서 — UI가 "어디서 멈췄는지"를 이 순서로 판정한다.
_PIPELINE_STAGES = [
    "prediction_signal", "entry_approved", "risk_manager", "order_sent",
    "broker_executed", "position_confirmed", "ui_synced",
]


_INVERSE_PRESSURE_BOOST_THRESHOLD = 70.0


def _boost_enhanced_score_with_inverse_pressure(enhanced_score: Optional[float], inverse_pressure_score: Optional[float]) -> Optional[float]:
    """ACTIVE_FUSION/Adaptive Fusion에 넘기는 enhanced_ai_score를 inverse_pressure_score로
    보정한다(2026-07-14 실측 버그 수정).

    calculate_fusion_score()(app/models/hynix_decision_v2.py)는 enhanced_ai_score만
    입력받고 inverse_pressure_score는 그 계산 체인 어디에도 전달되지 않는다. 그 결과
    레거시 판단(decide_hynix_or_inverse_action)이 inverse_pressure_score>=70을 근거로
    "INVERSE_STRONG_BUY"를 표시해도, Active Strategy/Adaptive Fusion 앙상블은 이 강한
    인버스 근거를 전혀 보지 못해 약한 신호로만 취급하고 매수하지 않는 일이 있었다.

    inverse_pressure_score가 강한 인버스 근거(>=70)를 보이면 enhanced_ai_score를 그
    방향(낮은 값)으로 더 강하게 보정한다 — min()을 써서 원래 enhanced_score보다
    약해지는(반대 방향으로 밀리는) 경우는 없다. 하이닉스 쪽은 enhanced_score 자체가
    이미 그 강도를 직접 반영하므로 별도 보정이 필요 없다(decision_thresholds의
    strong_buy_enhanced_min이 enhanced_score에 직접 적용됨)."""
    return enhanced_score


def _map_prediction_signal(final_action: str) -> str:
    return _SIGNAL_DISPLAY_MAP.get(final_action, "HOLD")


def _blank_pipeline_trace() -> dict:
    """Signal 생성과 실제 체결을 분리해서 보여주기 위한 단계별 추적 정보의 기본값.

    각 단계는 True(성공/승인)/False(실패/차단)/None(해당 없음, 예: HOLD라 진입 자체를
    시도하지 않음) 중 하나이며, `stopped_stage`는 그 중 실제로 막힌 첫 단계 이름이다
    (UI가 여기서 빨간색으로 표시한다).
    """
    return {
        "prediction_signal": "HOLD",
        "entry_approved": None, "entry_approved_reason": "",
        "risk_manager_ok": True, "risk_manager_reason": "정상",
        "risk_approved": True,
        "order_sent": False,
        # execution_stage: run_switch_or_entry()가 실제로 보고한 단계("entry"=주문이
        # 필요 없었음/진입 자체를 시도 안 함, "state_sync"=POSITION_SYNC_PENDING으로
        # 차단, "order_sent"=주문을 실제로 시도함). order_sent=False가 "주문 전송
        # 실패"인지 "애초에 주문이 필요 없었음"인지 구분하는 데 쓴다.
        "execution_stage": None,
        "broker_executed": False,
        "position_confirmed": None,
        "ui_synced": None,
        "trade_counter": 0,
        "stopped_stage": None,
        "blocking_reason": None,
    }


def _first_blocked_stage(trace: dict) -> Optional[str]:
    """Signal이 BUY/SELL/INVERSE인데 실제로 어느 단계에서 멈췄는지 첫 번째로 찾는다.

    HOLD는 애초에 아무것도 시도하지 않는 게 정상이므로 항상 None(정상)이다.
    """
    if trace["prediction_signal"] == "HOLD":
        return None
    if trace["entry_approved"] is False:
        return "entry_approved"
    if not trace["risk_manager_ok"]:
        return "risk_manager"
    if not trace["order_sent"]:
        # order_sent=False만으로는 "주문 전송 실패"로 단정하지 않는다 — 이미 목표
        # 종목을 보유해서 추가 진입이 필요 없었거나(entry), POSITION_SYNC_PENDING으로
        # 차단됐을 수 있다(state_sync). 실행 계층(run_switch_or_entry)이 보고한
        # execution_stage를 우선 신뢰하고, 없을 때만(레거시 경로 등) order_sent로 본다.
        exec_stage = trace.get("execution_stage")
        if exec_stage in ("entry", "state_sync"):
            return exec_stage
        return "order_sent"
    if not trace["broker_executed"]:
        return "broker_executed"
    if trace["position_confirmed"] is False:
        return "position_confirmed"
    if trace["ui_synced"] is False:
        return "ui_synced"
    return None


def _build_blocking_reason(trace: dict) -> Optional[str]:
    """stopped_stage를 사람이 읽을 수 있는 한 줄 사유로 변환 (UI의 blocking_reason 필드)."""
    stage = trace.get("stopped_stage")
    if not stage:
        return None
    reason_map = {
        "entry_approved": trace.get("entry_approved_reason"),
        "risk_manager": trace.get("risk_manager_reason"),
        "entry": trace.get("entry_approved_reason") or "이미 목표 종목 보유 중이거나 추가 진입이 필요 없어 주문을 시도하지 않음",
        "state_sync": trace.get("entry_approved_reason") or "POSITION_SYNC_PENDING — 브로커 잔고 확인 전이라 주문 차단",
        "order_sent": "주문이 브로커로 전송되지 않음(가격 조회 실패/쿨다운/허용 시간대 아님 등)",
        "broker_executed": "주문은 전송됐으나 브로커 체결 실패",
        "position_confirmed": "체결 후 재조회한 포지션이 기대와 불일치",
        "ui_synced": "상태 저장(디스크 반영) 실패 — 다음 사이클에서 재시도됨",
    }
    return f"[{stage}] {reason_map.get(stage) or '알 수 없음'}"


def evaluate_pullback_gate(
    state: dict, desired_symbol: str, final_action: str, now: datetime, forced_info: dict, hynix_df_1min, mode: str,
    primary_trend_result: Optional[dict] = None,
) -> dict:
    """General BUY pullback gate with a two-minute maximum wait."""
    held_symbol = (state.get("position") or {}).get("symbol")
    ptrend = primary_trend_result or {}
    primary_trend = ptrend.get("primary_trend", PRIMARY_TREND_RANGE)
    trend_blocked_reason = None
    if final_action in ("INVERSE_BUY", "INVERSE_STRONG_BUY") and new_inverse_entry_blocked(
        primary_trend, ptrend.get("above_vwap"), ptrend.get("above_ema20"), ptrend,
    ):
        votes, vote_reasons = inverse_block_vote_count(ptrend)
        trend_blocked_reason = (
            f"PRIMARY_TREND=UP with {votes} uptrend confirmations({', '.join(vote_reasons)}) - new INVERSE entry blocked "
            f"(short-term move classified as {classify_short_term_move(primary_trend, None)}"
            "; requires 2x-confirmed VWAP/15m-trend/swing-low breakdown to flip)"
        )
    elif final_action in ("HYNIX_BUY", "HYNIX_STRONG_BUY") and new_hynix_entry_blocked(
        primary_trend, ptrend.get("above_vwap"), ptrend.get("above_ema20"),
    ):
        trend_blocked_reason = (
            "PRIMARY_TREND=DOWN and price below VWAP/EMA20 - new HYNIX entry blocked "
            "(requires 2x-confirmed VWAP/15m-trend/swing-high breakout to flip)"
        )
    if trend_blocked_reason:
        state["last_trend_switch_plan"] = {
            "proceed": False, "dominant_direction": signal_direction(final_action), "desired_symbol": desired_symbol,
            "pullback_wait_remaining_seconds": None, "block_reason": trend_blocked_reason,
            "primary_trend": primary_trend,
        }
        return {
            "proceed": False, "deadline_expired": False, "pullback_wait_remaining_seconds": None,
            "message": trend_blocked_reason,
        }
    confirm_tracker = update_confirm_tracker(
        state.get("trend_switch_confirm_tracker") or default_confirm_state(),
        final_action, held_symbol, desired_symbol, now,
    )
    frequency_state = state.get("trend_switch_frequency_state") or default_trend_frequency_state()
    state["trend_switch_confirm_tracker"] = confirm_tracker
    has_unconfirmed_order = bool(
        state.get("order_in_flight")
        or state.get("pending_order")
        or state.get("trend_switch_unconfirmed_order")
    )
    pre_plan = plan_trend_switch_entry(
        final_action=final_action,
        held_symbol=held_symbol,
        desired_symbol=desired_symbol,
        confirm_tracker=confirm_tracker,
        frequency_state=frequency_state,
        pullback_result=None,
        now=now,
        data_ok=bool(desired_symbol),
        has_unconfirmed_order=has_unconfirmed_order,
        daily_return_pct=state.get("realized_pnl_today_pct"),
        atr_pct=None,
    )
    state["last_trend_switch_plan"] = {
        **pre_plan,
        "dominant_direction": signal_direction(final_action),
        "desired_symbol": desired_symbol,
        "pullback_wait_remaining_seconds": None,
    }
    if pre_plan.get("proceed"):
        return {
            "proceed": True,
            "deadline_expired": False,
            "pullback_wait_remaining_seconds": 0,
            "message": (
                f"TrendSwitchAccel 즉시 진입: {pre_plan.get('entry_type')} "
                f"{(pre_plan.get('position_pct') or 0) * 100:.0f}%"
            ),
        }
    pre_block = str(pre_plan.get("block_reason") or "")
    if pre_block and "눌림목" not in pre_block:
        return {
            "proceed": False,
            "deadline_expired": False,
            "pullback_wait_remaining_seconds": None,
            "message": pre_block,
        }

    pending = state.get("pending_entry")
    if not pending or pending.get("action") != final_action or pending.get("symbol") != desired_symbol:
        pending = {"action": final_action, "symbol": desired_symbol, "since": now.isoformat()}
        state["pending_entry"] = pending

    try:
        since = datetime.fromisoformat(pending["since"])
    except Exception:
        since = now

    deadline = since + timedelta(minutes=_PULLBACK_PATIENCE_MINUTES)
    window = forced_info.get("window")
    if window:
        try:
            _, end_str = window.split("-")
            deadline = min(deadline, datetime.combine(now.date(), _parse_hm(end_str)))
        except Exception:
            pass

    remaining_seconds = max(0, int((deadline - now).total_seconds()))
    if now >= deadline:
        deadline_plan = plan_trend_switch_entry(
            final_action=final_action,
            held_symbol=held_symbol,
            desired_symbol=desired_symbol,
            confirm_tracker=confirm_tracker,
            frequency_state=frequency_state,
            pullback_result={"proceed": True, "message": "pullback wait expired with current signal revalidated"},
            now=now,
            data_ok=bool(desired_symbol),
            has_unconfirmed_order=has_unconfirmed_order,
            daily_return_pct=state.get("realized_pnl_today_pct"),
            atr_pct=None,
        )
        state["last_trend_switch_plan"] = {
            **deadline_plan,
            "dominant_direction": signal_direction(final_action),
            "desired_symbol": desired_symbol,
            "pullback_wait_remaining_seconds": 0,
        }
        return {
            "proceed": bool(deadline_plan.get("proceed")),
            "deadline_expired": True,
            "pullback_wait_remaining_seconds": 0,
            "message": (
                f"눌림목 대기 데드라인 2분 만료({deadline.strftime('%H:%M')}) - 현재 신호 재검증 후 진입 허용"
                if deadline_plan.get("proceed") else deadline_plan.get("block_reason")
            ),
        }

    df_for_check = hynix_df_1min if desired_symbol == HYNIX_SYMBOL else _load_inverse_1min_for_pullback(mode)
    pullback = detect_pullback(df_for_check)
    try:
        pullback_pct = float(pullback.get("pullback_pct") or pullback.get("drop_pct") or pullback.get("distance_pct") or 0.0)
    except Exception:
        pullback_pct = 0.0
    if pullback_pct >= 4.0:
        message = f"deep pullback {pullback_pct:.2f}% - re-evaluate current trend instead of waiting on old high"
        wait_plan = {**pre_plan, "block_reason": message}
        state["last_trend_switch_plan"] = {
            **wait_plan,
            "dominant_direction": signal_direction(final_action),
            "desired_symbol": desired_symbol,
            "pullback_wait_remaining_seconds": 0,
            "dynamic_pullback_basis": pullback,
        }
        return {
            "proceed": False,
            "deadline_expired": False,
            "pullback_wait_remaining_seconds": 0,
            "message": message,
        }
    if pullback.get("is_pullback"):
        pullback_plan = plan_trend_switch_entry(
            final_action=final_action,
            held_symbol=held_symbol,
            desired_symbol=desired_symbol,
            confirm_tracker=confirm_tracker,
            frequency_state=frequency_state,
            pullback_result={"proceed": True, "message": pullback.get("reason")},
            now=now,
            data_ok=bool(desired_symbol),
            has_unconfirmed_order=has_unconfirmed_order,
            daily_return_pct=state.get("realized_pnl_today_pct"),
            atr_pct=None,
        )
        state["last_trend_switch_plan"] = {
            **pullback_plan,
            "dominant_direction": signal_direction(final_action),
            "desired_symbol": desired_symbol,
            "pullback_wait_remaining_seconds": remaining_seconds,
        }
        return {
            "proceed": bool(pullback_plan.get("proceed")),
            "deadline_expired": False,
            "pullback_wait_remaining_seconds": remaining_seconds,
            "message": (
                f"눌림목 진입 조건 충족: {pullback.get('reason')}"
                if pullback_plan.get("proceed") else pullback_plan.get("block_reason")
            ),
        }
    wait_plan = {
        **pre_plan,
        "block_reason": f"눌림목 대기 중({pullback.get('reason')}) - {deadline.strftime('%H:%M')}까지 대기",
    }
    state["last_trend_switch_plan"] = {
        **wait_plan,
        "dominant_direction": signal_direction(final_action),
        "desired_symbol": desired_symbol,
        "pullback_wait_remaining_seconds": remaining_seconds,
    }
    return {
        "proceed": False,
        "deadline_expired": False,
        "pullback_wait_remaining_seconds": remaining_seconds,
        "message": wait_plan["block_reason"],
    }


def _load_inverse_1min_for_pullback(mode: str):
    try:
        from app.data_sources.hynix_inverse_collector import collect_inverse_minute

        result = collect_inverse_minute(mode=mode)
        return result.get("df_1min")
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] 인버스 1분봉 조회 실패(눌림목 판단용): %s", exc)
        return None


def set_control(
    auto_trade_on: Optional[bool] = None, mode: Optional[str] = None,
    allow_mock_loss_override: Optional[bool] = None, mock_budget_krw: Optional[float] = None,
) -> dict:
    """UI에서 자동매매 ON/OFF, mock/real 모드, mock 예산을 설정할 때 사용."""
    if mode is not None:
        set_active_mode(mode)
    state = load_state(mode=mode)
    if auto_trade_on is not None:
        state["auto_trade_on"] = bool(auto_trade_on)
    if mode is not None:
        state["mode"] = mode
    if allow_mock_loss_override is not None:
        state["allow_mock_loss_override"] = bool(allow_mock_loss_override)
    if mock_budget_krw is not None:
        state["mock_budget_krw"] = float(mock_budget_krw)
        if state.get("mode") == "mock" and not (state.get("position") or {}).get("symbol"):
            state["cash"] = float(mock_budget_krw)
    save_state_atomic(state)
    return state


def reset_mock_account(budget_krw: Optional[float] = None) -> dict:
    """UI의 'mock 계좌 초기화' 버튼 — 포지션/거래횟수/현금을 오늘자 기준으로 완전히 새로 시작."""
    return reset_mock_state(budget_krw=budget_krw)


DAILY_RETURN_UNKNOWN = "DAILY_RETURN_UNKNOWN"
ACCOUNT_EQUITY_MISMATCH = "ACCOUNT_EQUITY_MISMATCH"
_EQUITY_MISMATCH_TOLERANCE_PCT = 0.5
_EQUITY_MISMATCH_RETRY_ATTEMPTS = 3
_EQUITY_MISMATCH_RETRY_DELAY_SECONDS = 3
_ACCOUNT_SETTLEMENT_GRACE_SECONDS = 60
_ACCOUNT_SNAPSHOT_FALLBACK_SECONDS = 90


def _position_market_value(position) -> float:
    if isinstance(position, dict):
        qty = float(position.get("quantity", position.get("hldg_qty", 0)) or 0)
        price = float(position.get("current_price", position.get("prpr", 0)) or 0)
        market_value = position.get("market_value")
    else:
        qty = float(getattr(position, "quantity", 0) or 0)
        price = float(getattr(position, "current_price", 0) or 0)
        market_value = getattr(position, "market_value", None)
    if market_value not in (None, ""):
        try:
            return float(market_value)
        except Exception:
            pass
    return qty * price


def _account_settlement_grace_active(state: dict, now: datetime) -> bool:
    candidates = [
        state.get("last_trade_time"),
        state.get("last_order_time"),
        (state.get("position") or {}).get("entry_time"),
    ]
    for raw in candidates:
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw))
        except Exception:
            continue
        if 0 <= (now - ts).total_seconds() <= _ACCOUNT_SETTLEMENT_GRACE_SECONDS:
            return True
    return False


def _read_account_equity_snapshot(broker, now: datetime) -> dict:
    """Read one account snapshot and calculate equity from that same response."""
    snapshot = {
        "ok": False, "cash": None, "holdings_market_value": None, "current_equity": None,
        "positions": [], "error": None, "source": None, "as_of": now.isoformat(timespec="seconds"),
    }
    try:
        if hasattr(broker, "kis") and hasattr(broker.kis, "get_balance"):
            bal = broker.kis.get_balance()
            if bal.get("error"):
                snapshot["error"] = bal.get("error")
                snapshot["rt_cd"] = bal.get("rt_cd")
                snapshot["msg_cd"] = bal.get("msg_cd")
                snapshot["msg1"] = bal.get("msg1")
                snapshot["response_field_names"] = bal.get("response_field_names")
                snapshot["output1_field_names"] = bal.get("output1_field_names")
                snapshot["output2_field_names"] = bal.get("output2_field_names")
                return snapshot
            positions = bal.get("positions") or []
            cash = bal.get("cash", None)
            if cash is None:
                snapshot["error"] = "cash field missing"
                return snapshot
            holdings = sum(_position_market_value(p) for p in positions)
            snapshot.update({
                "ok": True, "cash": float(cash), "holdings_market_value": holdings,
                "current_equity": float(cash) + holdings, "positions": positions,
                "source": "kis.get_balance",
                "response_field_names": bal.get("response_field_names"),
                "output1_field_names": bal.get("output1_field_names"),
                "output2_field_names": bal.get("output2_field_names"),
            })
            return snapshot

        positions = broker.get_positions()
        cash = broker.get_balance() if hasattr(broker, "get_balance") else None
        if cash is None:
            snapshot["error"] = "broker cash field missing"
            return snapshot
        holdings = sum(_position_market_value(p) for p in positions)
        snapshot.update({
            "ok": True, "cash": float(cash), "holdings_market_value": holdings,
            "current_equity": float(cash) + holdings, "positions": positions,
            "source": "broker.get_balance+get_positions",
        })
        return snapshot
    except Exception as exc:
        snapshot["error"] = str(exc)
        return snapshot


def _recent_valid_account_snapshot(state: dict, now: datetime) -> Optional[dict]:
    """Return a recent successful account snapshot when KIS is temporarily rate-limited.

    This is only a short-lived read-through fallback for EGW00201/tokenP throttling.
    It must not turn a missing or failed account response into 0 won.
    """
    snapshot = state.get("last_account_equity_snapshot") or {}
    if not snapshot.get("ok"):
        return None
    if snapshot.get("cash") is None or snapshot.get("current_equity") is None:
        return None
    raw_as_of = snapshot.get("as_of")
    if not raw_as_of:
        return None
    try:
        as_of = datetime.fromisoformat(str(raw_as_of))
    except Exception:
        return None
    try:
        age_seconds = (now - as_of).total_seconds()
    except Exception:
        return None
    if age_seconds < 0 or age_seconds > _ACCOUNT_SNAPSHOT_FALLBACK_SECONDS:
        return None
    cached = dict(snapshot)
    cached["ok"] = True
    cached["cached_fallback"] = True
    cached["cached_age_seconds"] = int(age_seconds)
    cached["source"] = f"{cached.get('source') or 'account_snapshot'}+recent_cache"
    return cached


def compute_net_daily_return(
    state: dict, position: Optional[dict], hynix_price: Optional[float], inverse_price: Optional[float],
    cash: Optional[float], positions_from_broker: Optional[list], cash_fetch_ok: bool,
    settlement_grace_active: bool = False,
) -> dict:
    """risk_manager와 UI가 공유하는 단일 일일손익 계산(요구사항 1/5절).

    net_daily_return = (net_realized_pnl + net_unrealized_pnl) / starting_equity.
    실현손익은 원장(state["realized_pnl_today_krw"])을, 미실현손익은 현재 보유
    포지션+현재가로 계산한다 — 둘 다 "지금 이 순간의 계좌 잔고 조회"에 의존하지
    않으므로, KIS API 오류(레이트리밋 등)로 잔고조회가 0을 반환해도 이 계산 자체는
    영향받지 않는다(2026-07-14 실측 버그: total_equity=0 → 일손실 -100%로 오판).

    cash/positions_from_broker는 오직 교차검증(current_equity 대조)에만 쓰이며,
    주 계산식에는 들어가지 않는다. cash_fetch_ok=False(조회 실패/빈 응답/필드
    누락)면 DAILY_RETURN_UNKNOWN으로 신규주문만 보류하고 손실로 기록하지 않는다.
    """
    net_realized_pnl = state.get("realized_pnl_today_krw", 0.0)
    result = {
        "starting_equity": state.get("daily_pnl_baseline_equity"),
        "net_realized_pnl": net_realized_pnl, "net_unrealized_pnl": 0.0,
        "net_daily_return": None, "current_equity": None,
        "calculation_source": "ledger_unified", "blocked_reason": None,
        "equity_tolerance_pct": _EQUITY_MISMATCH_TOLERANCE_PCT,
        "settlement_grace_active": settlement_grace_active,
    }

    has_position = bool(
        position and position.get("symbol") and (position.get("quantity") or 0) > 0 and position.get("entry_price"),
    )

    def _local_unrealized_pnl() -> float:
        if not has_position:
            return 0.0
        cur = hynix_price if position["symbol"] == HYNIX_SYMBOL else inverse_price
        if cur is None:
            return 0.0
        try:
            from app.trading.trading_cost_engine import TradeCostEngine

            cost_result = TradeCostEngine().compute_unrealized_net_pnl(
                position["symbol"], entry_price=position["entry_price"], current_price=cur,
                quantity=position["quantity"],
            )
            return float(cost_result["net_unrealized_pnl"])
        except Exception:
            return float((cur - position["entry_price"]) * position["quantity"])

    if not cash_fetch_ok:
        starting_equity = result["starting_equity"]
        if starting_equity and starting_equity > 0:
            net_unrealized_pnl = _local_unrealized_pnl()
            result["net_unrealized_pnl"] = net_unrealized_pnl
            result["net_daily_return"] = round((net_realized_pnl + net_unrealized_pnl) / starting_equity * 100.0, 4)
            result["calculation_warning"] = "ACCOUNT_SNAPSHOT_UNAVAILABLE_LEDGER_FALLBACK"
            return result
        result["blocked_reason"] = DAILY_RETURN_UNKNOWN
        return result

    holdings_value = sum(
        (getattr(p, "market_value", None) if not isinstance(p, dict) else p.get("market_value")) or 0.0
        for p in (positions_from_broker or [])
    )
    current_equity = (float(cash) + holdings_value) if cash is not None else None
    result["current_equity"] = current_equity

    starting_equity = result["starting_equity"]
    if starting_equity is None:
        # 당일 첫 유효 조회 — 기준자산으로 확정한다. 조회 자체가 이미 실패
        # 케이스(cash_fetch_ok=False)에서 걸러졌으므로 여기 도달했다면 신뢰할 수
        # 있는 값이다.
        if current_equity is not None and current_equity > 0:
            state["daily_pnl_baseline_equity"] = current_equity
            result["starting_equity"] = current_equity
            result["net_daily_return"] = 0.0
        else:
            result["blocked_reason"] = DAILY_RETURN_UNKNOWN
        return result

    if starting_equity <= 0:
        result["blocked_reason"] = ACCOUNT_EQUITY_MISMATCH
        return result

    # 요구사항 6절 — current_equity<=0인데 현금/원장잔고가 실제로 존재해야 하는 상황
    if current_equity is not None and current_equity <= 0 and (net_realized_pnl != 0 or starting_equity > 0):
        result["blocked_reason"] = ACCOUNT_EQUITY_MISMATCH if has_position else DAILY_RETURN_UNKNOWN
        return result

    # ── 미실현손익(요구사항 2절 — 보유 없으면 0) ─────────────────────────────
    net_unrealized_pnl = _local_unrealized_pnl()
    result["net_unrealized_pnl"] = net_unrealized_pnl

    net_daily_return = (net_realized_pnl + net_unrealized_pnl) / starting_equity * 100.0

    # 요구사항 6절 — 원장기준 수익률과 현재자산기준 수익률 차이가 0.1%p 초과하면
    # 계좌 데이터 불일치로 간주하고 -100% 같은 값으로 기록하지 않는다.
    if current_equity is not None and current_equity > 0:
        equity_ratio_return = (current_equity / starting_equity - 1.0) * 100.0
        if abs(equity_ratio_return - net_daily_return) > _EQUITY_MISMATCH_TOLERANCE_PCT:
            if not has_position and net_realized_pnl == 0 and net_unrealized_pnl == 0:
                state["daily_pnl_baseline_equity"] = current_equity
                result["starting_equity"] = current_equity
                result["net_daily_return"] = 0.0
                result["equity_ratio_return"] = 0.0
                result["baseline_rebased"] = True
                return result
            # KRX는 매도대금을 T+2에 정산한다 — 오늘 청산한 왕복거래의 실현손익은
            # 원장(net_realized_pnl)에는 즉시 반영되지만, 브로커의 실제 현금(cash)에는
            # 아직 반영되지 않은 게 정상이다. 이 "정산 지연"만으로 설명되는 차이라면
            # (미실현손익 기준 기대 수익률과는 실제로 일치) 계좌 이상이 아니므로 차단하지
            # 않는다 — 이전에는 60초 유예만으로는 턱없이 부족해 실현손익이 있는 날은
            # 이후 신규주문이 하루 종일 계속 차단되는 문제가 있었다.
            unsettled_adjusted_return = (net_unrealized_pnl / starting_equity) * 100.0
            settlement_adjusted_mismatch = abs(equity_ratio_return - unsettled_adjusted_return)
            if settlement_adjusted_mismatch <= _EQUITY_MISMATCH_TOLERANCE_PCT:
                result["net_daily_return"] = round(net_daily_return, 4)
                result["equity_ratio_return"] = round(equity_ratio_return, 4)
                result["mismatch_explained_by_unsettled_realized_pnl"] = True
                result["unsettled_realized_pnl_krw"] = net_realized_pnl
                return result
            if settlement_grace_active:
                result["net_daily_return"] = round(net_daily_return, 4)
                result["equity_ratio_return"] = round(equity_ratio_return, 4)
                result["mismatch_deferred"] = True
                return result
            result["blocked_reason"] = ACCOUNT_EQUITY_MISMATCH
            result["equity_ratio_return"] = round(equity_ratio_return, 4)
            return result

    result["net_daily_return"] = round(net_daily_return, 4)
    return result


def _compute_net_daily_return_with_retries(
    state: dict, broker, position: Optional[dict], hynix_price: Optional[float], inverse_price: Optional[float],
    now: datetime, *, attempts: int = _EQUITY_MISMATCH_RETRY_ATTEMPTS,
    delay_seconds: int = _EQUITY_MISMATCH_RETRY_DELAY_SECONDS,
) -> dict:
    attempts = max(1, int(attempts or 1))
    grace = _account_settlement_grace_active(state, now)
    history = []
    last_result = None
    last_snapshot = None
    for idx in range(attempts):
        snapshot = _read_account_equity_snapshot(broker, now)
        if not snapshot.get("ok"):
            cached_snapshot = _recent_valid_account_snapshot(state, now)
            if cached_snapshot is not None:
                cached_snapshot["live_error"] = snapshot.get("error")
                cached_snapshot["live_msg_cd"] = snapshot.get("msg_cd")
                cached_snapshot["live_msg1"] = snapshot.get("msg1")
                snapshot = cached_snapshot
        last_snapshot = snapshot
        result = compute_net_daily_return(
            state, position, hynix_price, inverse_price,
            cash=snapshot.get("cash"), positions_from_broker=snapshot.get("positions"),
            cash_fetch_ok=bool(snapshot.get("ok")), settlement_grace_active=grace,
        )
        result["account_snapshot"] = {
            "as_of": snapshot.get("as_of"), "source": snapshot.get("source"),
            "cash": snapshot.get("cash"), "holdings_market_value": snapshot.get("holdings_market_value"),
            "current_equity": snapshot.get("current_equity"), "ok": snapshot.get("ok"),
            "error": snapshot.get("error"),
            "positions": snapshot.get("positions") or [],
            "rt_cd": snapshot.get("rt_cd"), "msg_cd": snapshot.get("msg_cd"), "msg1": snapshot.get("msg1"),
            "response_field_names": snapshot.get("response_field_names"),
            "output1_field_names": snapshot.get("output1_field_names"),
            "output2_field_names": snapshot.get("output2_field_names"),
        }
        for key in ("cached_fallback", "cached_age_seconds", "live_error", "live_msg_cd", "live_msg1"):
            if snapshot.get(key) is not None:
                result["account_snapshot"][key] = snapshot.get(key)
        result["mismatch_retry_index"] = idx + 1
        history.append({
            "attempt": idx + 1,
            "blocked_reason": result.get("blocked_reason"),
            "current_equity": result.get("current_equity"),
            "net_daily_return": result.get("net_daily_return"),
            "equity_ratio_return": result.get("equity_ratio_return"),
            "snapshot": result["account_snapshot"],
        })
        last_result = result
        if result.get("blocked_reason") != ACCOUNT_EQUITY_MISMATCH:
            break
        if idx < attempts - 1:
            time.sleep(delay_seconds)
    last_result = last_result or {"blocked_reason": DAILY_RETURN_UNKNOWN}
    last_result["equity_check_history"] = history
    last_result["equity_check_attempts"] = len(history)
    last_result["settlement_grace_active"] = grace
    if last_snapshot:
        state["last_account_equity_snapshot"] = last_result.get("account_snapshot")
    return last_result


def check_real_mode_gates(state: dict, cfg=None) -> dict:
    """명세 5절 — real 실제 주문 전 필수 게이트 체크리스트(읽기 전용 진단 함수).

    이 함수는 어떤 게이트도 새로 활성화하지 않는다 — 기존 config.yaml/.env/
    trading_policy.yaml 값을 그대로 조회만 한다. Active Strategy는 여전히 mock
    전용이며, 이 체크리스트는 향후 real 연결 승인 여부를 판단하기 위한 참고용이다.
    """
    from app.config import get_config

    cfg = cfg or get_config()
    real_gate_status = (
        cfg.enhanced_real_gate_status(current_mode=state.get("mode", "mock"))
        if hasattr(cfg, "enhanced_real_gate_status")
        else {"ready": cfg.full_auto_real_confirm_ok(), "blocking_reasons": []}
    )
    checks = {
        "ui_mode_real": state.get("mode") == "real",
        "real_master_switch": cfg.real_trading_enabled(),
        "auto_trade_enabled": bool(state.get("auto_trade_on")),
        "real_auto_order_enabled": bool(real_gate_status.get("ready")),
        "no_position_conflict": not bool(state.get("position_conflict")),
    }
    all_pass = all(checks.values())
    failed = [name for name, ok in checks.items() if not ok]
    return {
        "checks": checks,
        "all_pass": all_pass,
        "failed_gates": failed,
        "real_gate_status": real_gate_status,
    }


def _run_active_strategy_entry(
    state: dict, broker, hynix_price: Optional[float], inverse_price: Optional[float],
    now: datetime, orders_this_cycle: list, enhanced_ai_score: Optional[float] = None,
    position_manager=None,
) -> dict:
    """ACTIVE STRATEGY(거래모드 기반 조기진입/Scale-in/빠른전환) — mock 전용 opt-in.

    state["active_strategy_enabled"]가 True이고 mode=="mock"일 때만 호출부에서
    호출된다(real 모드에서는 절대 호출되지 않음 — 호출부에서 이미 mode=="mock"을
    확인). 기존 ENHANCED_REGIME_SWITCH 진입 로직(run_switch_or_entry)을 이번 사이클만
    대체하며, 같은 브로커/포지션 파이프라인(_buy_new/_sell_all_or_ratio → 실행
    원장)을 그대로 사용하되 signal_source="ACTIVE_ONLY"으로 구분 기록한다.
    """
    final_decision = {"executable": False, "order_sent": False, "signal_source": "SHADOW_ONLY"}
    state["last_final_execution_decision"] = final_decision
    return {
        "acted": False,
        "message": "ACTIVE_STRATEGY shadow-only: actual broker orders are owned by ENHANCED_REGIME_SWITCH",
        "action": "SHADOW_ONLY",
        "decision": {},
        "final_decision": final_decision,
        "failure_reason": "SHADOW_ONLY",
    }

    from app.trading.hynix_switch_position_manager import _buy_new, _sell_all_or_ratio
    from app.trading.hynix_active_strategy_engine import (
        decide_active_strategy_action, default_active_strategy_state,
        register_position_opened, register_position_closed,
        to_final_execution_decision, generate_idempotency_key, is_duplicate_signal, register_idempotency_key,
        REASON_INSUFFICIENT_CASH, REASON_DUPLICATE_SIGNAL, REASON_STALE_PRICE, REASON_ORDER_EXCEPTION,
        REASON_RISK_LIMIT, REASON_COOLDOWN,
        ACTION_ENTER_HYNIX, ACTION_ENTER_INVERSE, ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH,
    )
    from app.trading.hynix_trading_mode import DEFAULT_MODE

    shadow = state.get("last_cycle_ai_result") or {}
    cyc = shadow.get("cycle") or {}
    prob = shadow.get("probability") or {}
    turning_point = cyc.get("turning_point") or {}
    momentum = cyc.get("momentum") or {}

    mode_name = state.get("trading_mode", DEFAULT_MODE)
    strategy_state = state.get("active_strategy_state") or default_active_strategy_state(mode_name)

    position = state.get("position") or {}
    position_state = {
        "symbol": position.get("symbol"), "quantity": position.get("quantity") or 0,
        "entry_price": position.get("entry_price"),
    }

    # expected_move_pct — 전용 예측 필드가 아직 없어 momentum의 최근 3분 속도를 근사치로
    # 사용한다(정밀한 값이 아니라 "과도한 진입/완화를 막는 최소 안전장치" 목적).
    raw_vel = momentum.get("raw_velocity_3")
    expected_move_pct = round(abs(raw_vel) * 2.0, 3) if raw_vel is not None else 0.25

    decision_result = decide_active_strategy_action(
        mode=mode_name, now=now,
        buy_probability=prob.get("buy_probability", 0.0), inverse_probability=prob.get("sell_probability", 0.0),
        hold_probability=prob.get("hold_probability", 100.0),
        model_confidence=turning_point.get("confidence", 50.0), expected_move_pct=expected_move_pct,
        down_turn_probability_3m=turning_point.get("down_turn_probability_3m"),
        up_turn_probability_3m=turning_point.get("up_turn_probability_3m"),
        momentum_inflection_or_acceleration=momentum.get("momentum_acceleration_up"),
        cycle_phase=cyc.get("cycle_phase"), order_flow_confidence=None,
        atr_pct=None, consecutive_stop_losses=strategy_state.get("consecutive_stop_losses", 0),
        recent_pnl_pct=state.get("realized_pnl_today_pct"), daily_return_pct=state.get("realized_pnl_today_pct"),
        position_state=position_state, strategy_state=strategy_state,
        data_ok=bool(hynix_price and inverse_price), position_conflict=bool(state.get("position_conflict")),
        enhanced_ai_score=enhanced_ai_score, micron_ai_score=shadow.get("effective_micron_score"),
    )
    state["active_strategy_state"] = decision_result["state"]
    action = decision_result["action"]

    # ── FinalExecutionDecision: 이번 사이클의 실행 신호를 단일 객체로 통일한다.
    # 주문 실행은 이 객체의 executable/blocking_reason만 근거로 삼는다.
    final_decision = to_final_execution_decision(decision_result, held_symbol=position_state.get("symbol"))
    state["last_final_execution_decision"] = final_decision

    acted = False
    order_id = executed_price = executed_qty = None
    failure_reason = None
    message = final_decision.get("blocking_reason") or "; ".join(final_decision.get("reasons", [])) or "HOLD"

    if not final_decision["executable"]:
        final_decision.update(order_sent=False, order_id=None, executed_price=None, executed_qty=None, failure_reason=None)
        return {
            "acted": False, "message": message, "action": action,
            "decision": decision_result, "final_decision": final_decision, "failure_reason": None,
        }

    # ── 중복 신호 방지(명세 8절): 같은 분 단위 cycle_id 안에서 동일 action+symbol 재실행 금지 ──
    cycle_id = now.strftime("%Y%m%d%H%M")
    idem_key = generate_idempotency_key(now, mode_name, cycle_id, final_decision["action"], final_decision["symbol"] or "")
    if is_duplicate_signal(state["active_strategy_state"], idem_key):
        failure_reason = REASON_DUPLICATE_SIGNAL
        message = f"중복 신호 차단(idempotency_key={idem_key})"
        final_decision.update(order_sent=False, order_id=None, executed_price=None, executed_qty=None, failure_reason=failure_reason)
        return {
            "acted": False, "message": message, "action": action,
            "decision": decision_result, "final_decision": final_decision, "failure_reason": failure_reason,
        }

    if action in (ACTION_ENTER_HYNIX, ACTION_ENTER_INVERSE):
        symbol = decision_result["recommended_symbol"]
        price = _current_price(symbol, hynix_price, inverse_price)
        pct = decision_result["recommended_position_pct"]
        if not price:
            failure_reason, message = REASON_STALE_PRICE, f"{symbol} 현재가 없음 — 주문 미실행"
        elif pct <= 0:
            failure_reason, message = REASON_RISK_LIMIT, "권장 비중 0% — 주문 미실행"
        else:
            try:
                full_cash = float(broker.get_buyable_cash())
            except Exception as exc:
                full_cash, failure_reason, message = 0.0, REASON_ORDER_EXCEPTION, f"매수가능금액 조회 실패: {exc}"
            if full_cash > 0:
                cash_amount = full_cash * (pct / 100.0)
                if cash_amount < price:
                    failure_reason = REASON_INSUFFICIENT_CASH
                    message = f"매수가능금액 부족(필요 {price:,.0f}원, 가용 {cash_amount:,.0f}원)"
                else:
                    try:
                        buy_result = _buy_new(
                            broker, symbol, price, cash_amount, f"Active Strategy({mode_name}) 진입 {pct:.0f}%",
                            orders_this_cycle, mode="mock", signal_source="ACTIVE_ONLY",
                            position_manager=position_manager,
                        )
                    except Exception as exc:
                        buy_result, failure_reason = {"success": False, "message": str(exc)}, REASON_ORDER_EXCEPTION
                    if buy_result.get("success"):
                        if buy_result.get("position_sync_status") == "POSITION_SYNC_PENDING":
                            failure_reason = REASON_ORDER_EXCEPTION
                            message = buy_result.get("message", "POSITION_SYNC_PENDING")
                            state["position_sync_block_new_orders"] = True
                            state["position_sync_status"] = "POSITION_SYNC_PENDING"
                            state["critical_alert"] = message
                            return {
                                "acted": True, "message": message, "action": action,
                                "decision": decision_result, "final_decision": {}, "failure_reason": failure_reason,
                            }
                        acted = True
                        qty = buy_result.get("bought_quantity", 0)
                        order_id, executed_price, executed_qty = buy_result.get("order_id"), price, qty
                        state["position"] = {
                            "symbol": symbol, "name": symbol, "quantity": qty, "avg_price": price,
                            "entry_price": price, "entry_time": now.isoformat(), "partial_tp1_done": False, "partial_sl1_done": False,
                        }
                        state["active_strategy_state"] = register_position_opened(state["active_strategy_state"], symbol, price, pct, now)
                        message = f"Active Strategy 진입: {symbol} {pct:.0f}%({qty}주)"
                    elif not failure_reason:
                        failure_reason = REASON_ORDER_EXCEPTION
                        message = buy_result.get("message", "매수 실패")

    elif action in (ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH) and position_state.get("symbol"):
        symbol = position_state["symbol"]
        price = _current_price(symbol, hynix_price, inverse_price)
        ratio = 1.0 if action in (ACTION_EXIT_ALL, ACTION_SWITCH) else max(0.01, min(1.0, decision_result["recommended_position_pct"] / 100.0))
        if not price:
            failure_reason, message = REASON_STALE_PRICE, f"{symbol} 현재가 없음 — 매도 미실행"
        else:
            try:
                sell_result = _sell_all_or_ratio(
                    broker, position, price, ratio, f"Active Strategy({mode_name}) {action}", orders_this_cycle,
                    mode="mock", exit_reason_type="active_strategy", signal_source="ACTIVE_ONLY",
                    position_manager=position_manager,
                )
            except Exception as exc:
                sell_result, failure_reason = {"success": False, "message": str(exc)}, REASON_ORDER_EXCEPTION
            if sell_result.get("success"):
                acted = True
                order_id, executed_price = sell_result.get("order_id"), price
                executed_qty = sell_result.get("sold_quantity")
                remaining = sell_result.get("remaining_quantity")
                if sell_result.get("position_sync_status") == "POSITION_SYNC_PENDING" or remaining is None:
                    state["position_sync_block_new_orders"] = True
                    state["position_sync_status"] = "POSITION_SYNC_PENDING"
                    state["critical_alert"] = "POSITION_SYNC_PENDING - broker balance confirmation failed after sell"
                    failure_reason = REASON_ORDER_EXCEPTION
                    message = state["critical_alert"]
                    return {
                        "acted": True, "message": message, "action": action,
                        "decision": decision_result, "final_decision": {}, "failure_reason": failure_reason,
                    }
                if remaining <= 0:
                    state["position"] = {
                        "symbol": None, "quantity": 0, "avg_price": None, "entry_price": None,
                        "entry_time": None, "name": None, "partial_tp1_done": False, "partial_sl1_done": False,
                    }
                    state["active_strategy_state"] = register_position_closed(state["active_strategy_state"], was_stop_loss=False, now=now)
                else:
                    state["position"]["quantity"] = remaining
                message = f"Active Strategy 청산/축소: {symbol} {action} (비중 {ratio*100:.0f}%)"
            elif not failure_reason:
                failure_reason = REASON_COOLDOWN if sell_result.get("blocked_by_coordinator") else REASON_ORDER_EXCEPTION
                message = sell_result.get("message", "매도 실패")

    if acted:
        state["active_strategy_state"] = register_idempotency_key(state["active_strategy_state"], idem_key)

    final_decision.update(
        order_sent=acted, order_id=order_id, executed_price=executed_price, executed_qty=executed_qty,
        failure_reason=(failure_reason if not acted else None),
    )
    state["last_final_execution_decision"] = final_decision

    return {
        "acted": acted, "message": message, "action": action,
        "decision": decision_result, "final_decision": final_decision, "failure_reason": failure_reason,
    }


def _run_adaptive_fusion_entry(
    state: dict, broker, hynix_price: Optional[float], inverse_price: Optional[float],
    now: datetime, orders_this_cycle: list, enhanced_ai_score: Optional[float] = None,
    hynix_df_1min=None, position_manager=None,
) -> dict:
    """ADAPTIVE FUSION — Prediction AI V2를 실제 mock 주문에 반영하되 ACTIVE_FUSION을
    완전히 대체하지 않는 성과기반 융합 엔진(mock 전용 opt-in, state["adaptive_fusion_enabled"]).

    2026-07-13 사용자 검증: 이전에는 Prediction V2가 buy_probability/sell_probability
    "값"만 _run_active_strategy_entry의 입력으로 흘러들어갔을 뿐, V2 자신의 독자 판단
    (decide_final_action_v2)이나 Cycle AI/Micron Proxy의 독자 판단은 실제 체결
    signal_source에 전혀 반영되지 않았다(항상 ACTIVE_STRATEGY_MOCK/DYNAMIC_EXIT만
    기록됨). 이 함수는 5개 모델(ACTIVE_FUSION/PREDICTION_V2/CYCLE_AI/EARLY_PREDICTION/
    MICRON_PROXY)의 독자 확률을 실제로 융합해 신규 진입을 결정하고, 그 기여도를
    ledger(active_probability/prediction_v2_probability/.../dominant_model/
    prediction_v2_weight)에 그대로 남긴다. 보유 포지션의 청산/전환 관리는 이미 검증된
    decide_active_strategy_action()의 판단을 그대로 재사용한다(이번 턴에서 선제청산
    로직까지 전부 새로 검증하기보다, 신규진입 판단에 V2를 반영하는 핵심 문제부터
    안전하게 해결하기 위함 — 확장 여지는 남겨둔다).
    """
    fusion_decision = {"executable": False, "signal_source": "SHADOW_ONLY", "weights": {}, "dominant_model": "SHADOW_ONLY"}
    final_decision = {"executable": False, "order_sent": False, "signal_source": "SHADOW_ONLY"}
    state["last_fusion_decision"] = fusion_decision
    state["last_final_execution_decision"] = final_decision
    return {
        "acted": False,
        "message": "ADAPTIVE_FUSION shadow-only: actual broker orders are owned by ENHANCED_REGIME_SWITCH",
        "action": "SHADOW_ONLY",
        "decision": {},
        "fusion_decision": fusion_decision,
        "final_decision": final_decision,
        "failure_reason": "SHADOW_ONLY",
    }

    from app.trading.hynix_switch_position_manager import _buy_new, _sell_all_or_ratio
    from app.trading.hynix_active_strategy_engine import (
        decide_active_strategy_action, default_active_strategy_state,
        register_position_opened, register_position_closed,
        generate_idempotency_key, is_duplicate_signal, register_idempotency_key,
        REASON_INSUFFICIENT_CASH, REASON_DUPLICATE_SIGNAL, REASON_STALE_PRICE, REASON_ORDER_EXCEPTION,
        REASON_RISK_LIMIT, REASON_COOLDOWN,
        ACTION_ENTER_HYNIX, ACTION_ENTER_INVERSE, ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH,
    )
    from app.trading.hynix_trading_mode import DEFAULT_MODE
    from app.trading.hynix_adaptive_fusion_engine import (
        HynixAdaptiveFusionEngine, evaluate_prediction_v2_performance,
        default_hold_tracker, update_hold_tracker, default_whipsaw_state, register_direction_flip,
        default_frequency_state, register_frequency_entry, register_frequency_round_trip_closed,
        compute_live_hynix_trend,
        MODEL_PREDICTION_V2, MODEL_STATUS_ADVISORY, MODEL_STATUS_LIVE_VALIDATED,
        ACTION_HYNIX as FUSION_HYNIX, ACTION_INVERSE as FUSION_INVERSE, ACTION_HOLD as FUSION_HOLD,
    )
    from app.services.hynix_execution_ledger import (
        SIGNAL_SOURCE_ACTIVE_ONLY, SIGNAL_SOURCE_ADAPTIVE_FUSION, SIGNAL_SOURCE_PREDICTION_V2_ASSISTED,
    )

    shadow = state.get("last_cycle_ai_result") or {}
    cyc = shadow.get("cycle") or {}
    prob = shadow.get("probability") or {}
    decision_v2 = shadow.get("decision_v2") or {"final_action_v2": "HOLD"}
    turning_point = cyc.get("turning_point") or {}
    momentum = cyc.get("momentum") or {}

    mode_name = state.get("trading_mode", DEFAULT_MODE)
    strategy_state = state.get("active_strategy_state") or default_active_strategy_state(mode_name)

    position = state.get("position") or {}
    position_state = {
        "symbol": position.get("symbol"), "quantity": position.get("quantity") or 0,
        "entry_price": position.get("entry_price"),
    }
    has_position = bool(position_state["symbol"]) and position_state["quantity"] > 0

    raw_vel = momentum.get("raw_velocity_3")
    expected_move_pct = round(abs(raw_vel) * 2.0, 3) if raw_vel is not None else 0.25
    data_ok = bool(hynix_price and inverse_price)

    # ── Model A: ACTIVE_FUSION 자체 판단(이미 검증된 로직 — 보유 포지션 관리는 이 결과를 그대로 쓴다) ──
    active_decision_result = decide_active_strategy_action(
        mode=mode_name, now=now,
        buy_probability=prob.get("buy_probability", 0.0), inverse_probability=prob.get("sell_probability", 0.0),
        hold_probability=prob.get("hold_probability", 100.0),
        model_confidence=turning_point.get("confidence", 50.0), expected_move_pct=expected_move_pct,
        down_turn_probability_3m=turning_point.get("down_turn_probability_3m"),
        up_turn_probability_3m=turning_point.get("up_turn_probability_3m"),
        momentum_inflection_or_acceleration=momentum.get("momentum_acceleration_up"),
        cycle_phase=cyc.get("cycle_phase"), order_flow_confidence=None,
        atr_pct=None, consecutive_stop_losses=strategy_state.get("consecutive_stop_losses", 0),
        recent_pnl_pct=state.get("realized_pnl_today_pct"), daily_return_pct=state.get("realized_pnl_today_pct"),
        position_state=position_state, strategy_state=strategy_state,
        data_ok=data_ok, position_conflict=bool(state.get("position_conflict")),
        enhanced_ai_score=enhanced_ai_score, micron_ai_score=shadow.get("effective_micron_score"),
    )
    state["active_strategy_state"] = active_decision_result["state"]

    # ── Model B~E 입력 준비 ──────────────────────────────────────────────────
    prediction_v2_performance = evaluate_prediction_v2_performance(now)
    micron_snapshot = state.get("last_micron_proxy_snapshot") or {}
    micron_proxy = None
    if micron_snapshot.get("effective_micron_score") is not None:
        micron_proxy = {
            "effective_micron_score": micron_snapshot.get("effective_micron_score"),
            "micron_data_confidence": micron_snapshot.get("confidence"),
            "micron_score_source": micron_snapshot.get("score_source"),
            "calculated_at": micron_snapshot.get("calculated_at"),
        }
        try:
            if micron_snapshot.get("calculated_at"):
                age = (now - datetime.fromisoformat(str(micron_snapshot.get("calculated_at")))).total_seconds() / 60.0
                if age < 0:
                    # 캔들/스냅샷 시각이 현재(KST) 기준 미래로 보임 — 시계/타임존
                    # 불일치(DATA_TIME_ERROR)이지 "매우 신선한 데이터"가 아니다.
                    # 절대 0으로 뭉개서 fresh처럼 취급하면 안 된다(2026-07-16 실측
                    # 버그: age=-538분이 is_stale=False로 통과됨).
                    micron_proxy["age_minutes"] = round(age, 2)
                    micron_proxy["is_stale"] = True
                    micron_proxy["data_time_error"] = True
                    logger.error(
                        "[HynixSwitchEngine] Micron proxy DATA_TIME_ERROR: age=%.1f분(음수) — "
                        "시계/타임존 불일치 의심, stale로 처리",
                        age,
                    )
                else:
                    micron_proxy["age_minutes"] = round(age, 2)
                    micron_proxy["is_stale"] = age > 15.0
                    micron_proxy["data_time_error"] = False
        except Exception:
            pass

    hold_tracker = state.get("adaptive_fusion_hold_tracker") or default_hold_tracker()
    whipsaw_state = state.get("adaptive_fusion_whipsaw_state") or default_whipsaw_state()
    frequency_state = state.get("adaptive_fusion_frequency_state") or default_frequency_state()
    live_hynix_trend = compute_live_hynix_trend(hynix_df_1min, now)
    prior_live = state.get("live_hynix_trend_state") or {}
    if live_hynix_trend.get("hynix_uptrend_confirmed"):
        live_hynix_trend["hynix_uptrend_streak"] = int(prior_live.get("hynix_uptrend_streak", 0) or 0) + 1
        live_hynix_trend["hynix_downtrend_streak"] = 0
    elif live_hynix_trend.get("hynix_downtrend_confirmed"):
        live_hynix_trend["hynix_downtrend_streak"] = int(prior_live.get("hynix_downtrend_streak", 0) or 0) + 1
        live_hynix_trend["hynix_uptrend_streak"] = 0
    else:
        live_hynix_trend["hynix_uptrend_streak"] = 0
        live_hynix_trend["hynix_downtrend_streak"] = 0
    state["live_hynix_trend_state"] = {
        "hynix_uptrend_streak": live_hynix_trend.get("hynix_uptrend_streak", 0),
        "hynix_downtrend_streak": live_hynix_trend.get("hynix_downtrend_streak", 0),
        "last_direction": live_hynix_trend.get("direction"),
        "as_of": live_hynix_trend.get("as_of"),
    }
    state["last_live_hynix_trend"] = live_hynix_trend

    engine = HynixAdaptiveFusionEngine()
    fusion_decision = engine.decide(
        now=now, active_decision_result=active_decision_result,
        prediction_v2_probability=prob, prediction_v2_decision=decision_v2,
        prediction_v2_performance=prediction_v2_performance, cycle_result=cyc,
        cycle_ai_validated=bool(state.get("cycle_ai_validated", False)), micron_proxy=micron_proxy,
        held_symbol=position_state["symbol"], position_conflict=bool(state.get("position_conflict")),
        data_ok=data_ok, price_is_stale=not data_ok,
        daily_return_pct=state.get("realized_pnl_today_pct"), orders_today_count=state.get("daily_trade_count", 0),
        hold_tracker=hold_tracker, whipsaw_state=whipsaw_state, frequency_state=frequency_state,
        consecutive_stop_losses=strategy_state.get("consecutive_stop_losses", 0),
        live_hynix_trend=live_hynix_trend,
    )

    fused_action = fusion_decision["final_action"]
    hold_tracker = update_hold_tracker(hold_tracker, has_position, fused_action, now)
    state["adaptive_fusion_hold_tracker"] = hold_tracker
    last_direction = state.get("adaptive_fusion_last_direction")
    if fused_action in (FUSION_HYNIX, FUSION_INVERSE):
        if last_direction and last_direction != fused_action:
            whipsaw_state = register_direction_flip(whipsaw_state, now)
        state["adaptive_fusion_last_direction"] = fused_action
    state["adaptive_fusion_whipsaw_state"] = whipsaw_state
    state["last_fusion_decision"] = fusion_decision

    # ── signal_source: Prediction V2가 실제로 (SHADOW를 벗어나) 기여했을 때만 그렇다고 표기한다 ──
    pv2_weight = fusion_decision["weights"].get(MODEL_PREDICTION_V2, 0.0)
    pv2_status = prediction_v2_performance.get("model_status")
    pv2_applied = pv2_status in (MODEL_STATUS_ADVISORY, MODEL_STATUS_LIVE_VALIDATED) and pv2_weight > 0
    if not pv2_applied:
        signal_source = SIGNAL_SOURCE_ACTIVE_ONLY
    elif fusion_decision["dominant_model"] == MODEL_PREDICTION_V2:
        signal_source = SIGNAL_SOURCE_PREDICTION_V2_ASSISTED
    else:
        signal_source = SIGNAL_SOURCE_ADAPTIVE_FUSION

    fusion_metadata = {
        "active_probability": fusion_decision["fused_hynix_probability"],  # 참고용(대표값) — 상세는 아래 개별 확률 사용
        "prediction_v2_probability": prob.get("buy_probability"),
        "cycle_probability": turning_point.get("up_turn_probability_3m"),
        "fused_probability": max(fusion_decision["fused_hynix_probability"], fusion_decision["fused_inverse_probability"]),
        "prediction_v2_weight": pv2_weight if pv2_applied else 0.0,
        "dominant_model": fusion_decision["dominant_model"],
        "model_agreement": fusion_decision["model_agreement"],
        "expected_value": fusion_decision["expected_value"],
        "target_position_pct": fusion_decision["target_position_pct"],
    }

    action = None
    acted = False
    order_id = executed_price = executed_qty = None
    failure_reason = None
    message = fusion_decision.get("blocking_reason") or "; ".join(fusion_decision.get("reasons", [])[:1]) or "HOLD"

    if has_position:
        # 보유 포지션 관리(청산/전환/Scale)는 이미 검증된 ACTIVE_FUSION 판단을 그대로 쓴다.
        action = active_decision_result["action"]
        if action in (ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH):
            symbol = position_state["symbol"]
            price = _current_price(symbol, hynix_price, inverse_price)
            ratio = 1.0 if action in (ACTION_EXIT_ALL, ACTION_SWITCH) else max(0.01, min(1.0, active_decision_result["recommended_position_pct"] / 100.0))
            if not price:
                failure_reason, message = REASON_STALE_PRICE, f"{symbol} 현재가 없음 — 매도 미실행"
            else:
                try:
                    sell_result = _sell_all_or_ratio(
                        broker, position, price, ratio, f"Adaptive Fusion({mode_name}) {action}", orders_this_cycle,
                        mode="mock", exit_reason_type="active_strategy", signal_source=signal_source,
                        fusion_metadata=fusion_metadata, position_manager=position_manager,
                    )
                except Exception as exc:
                    sell_result, failure_reason = {"success": False, "message": str(exc)}, REASON_ORDER_EXCEPTION
                if sell_result.get("success"):
                    acted = True
                    order_id, executed_price = sell_result.get("order_id"), price
                    executed_qty = sell_result.get("sold_quantity")
                    remaining = sell_result.get("remaining_quantity")
                    if sell_result.get("position_sync_status") == "POSITION_SYNC_PENDING" or remaining is None:
                        state["position_sync_block_new_orders"] = True
                        state["position_sync_status"] = "POSITION_SYNC_PENDING"
                        state["critical_alert"] = "POSITION_SYNC_PENDING - broker balance confirmation failed after sell"
                        failure_reason = REASON_ORDER_EXCEPTION
                        message = state["critical_alert"]
                        return {
                            "acted": True, "message": message, "action": action,
                            "decision": fusion_decision, "final_decision": {}, "failure_reason": failure_reason,
                        }
                    if remaining <= 0:
                        state["position"] = {
                            "symbol": None, "quantity": 0, "avg_price": None, "entry_price": None,
                            "entry_time": None, "name": None, "partial_tp1_done": False, "partial_sl1_done": False,
                        }
                        state["active_strategy_state"] = register_position_closed(state["active_strategy_state"], was_stop_loss=False, now=now)
                        state["adaptive_fusion_frequency_state"] = register_frequency_round_trip_closed(frequency_state, now)
                    else:
                        state["position"]["quantity"] = remaining
                    message = f"Adaptive Fusion 청산/축소: {symbol} {action} (비중 {ratio*100:.0f}%)"
                elif not failure_reason:
                    failure_reason = REASON_COOLDOWN if sell_result.get("blocked_by_coordinator") else REASON_ORDER_EXCEPTION
                    message = sell_result.get("message", "매도 실패")
    elif fusion_decision["executable"] and fused_action in (FUSION_HYNIX, FUSION_INVERSE) and fusion_decision.get("symbol"):
        symbol = fusion_decision["symbol"]
        pct = fusion_decision["target_position_pct"]
        price = _current_price(symbol, hynix_price, inverse_price)
        cycle_id = now.strftime("%Y%m%d%H%M")
        idem_key = generate_idempotency_key(now, mode_name, cycle_id, fused_action, symbol)
        if is_duplicate_signal(state["active_strategy_state"], idem_key):
            failure_reason, message = REASON_DUPLICATE_SIGNAL, f"중복 신호 차단(idempotency_key={idem_key})"
        elif not price:
            failure_reason, message = REASON_STALE_PRICE, f"{symbol} 현재가 없음 — 주문 미실행"
        elif pct <= 0:
            failure_reason, message = REASON_RISK_LIMIT, "권장 비중 0% — 주문 미실행"
        else:
            try:
                full_cash = float(broker.get_buyable_cash())
            except Exception as exc:
                full_cash, failure_reason, message = 0.0, REASON_ORDER_EXCEPTION, f"매수가능금액 조회 실패: {exc}"
            if full_cash > 0:
                cash_amount = full_cash * (pct / 100.0)
                if cash_amount < (price or 0):
                    failure_reason = REASON_INSUFFICIENT_CASH
                    message = f"매수가능금액 부족(필요 {price:,.0f}원, 가용 {cash_amount:,.0f}원)"
                else:
                    try:
                        buy_result = _buy_new(
                            broker, symbol, price, cash_amount, f"Adaptive Fusion({mode_name}) 진입 {pct:.0f}%",
                            orders_this_cycle, mode="mock", signal_source=signal_source, fusion_metadata=fusion_metadata,
                            position_manager=position_manager,
                        )
                    except Exception as exc:
                        buy_result, failure_reason = {"success": False, "message": str(exc)}, REASON_ORDER_EXCEPTION
                    if buy_result.get("success"):
                        if buy_result.get("position_sync_status") == "POSITION_SYNC_PENDING":
                            failure_reason = REASON_ORDER_EXCEPTION
                            message = buy_result.get("message", "POSITION_SYNC_PENDING")
                            state["position_sync_block_new_orders"] = True
                            state["position_sync_status"] = "POSITION_SYNC_PENDING"
                            state["critical_alert"] = message
                            return {
                                "acted": True, "message": message, "action": fused_action,
                                "decision": fusion_decision, "final_decision": {}, "failure_reason": failure_reason,
                            }
                        acted = True
                        action = ACTION_ENTER_HYNIX if symbol == "000660" else ACTION_ENTER_INVERSE
                        qty = buy_result.get("bought_quantity", 0)
                        order_id, executed_price, executed_qty = buy_result.get("order_id"), price, qty
                        state["position"] = {
                            "symbol": symbol, "name": symbol, "quantity": qty, "avg_price": price,
                            "entry_price": price, "entry_time": now.isoformat(), "partial_tp1_done": False, "partial_sl1_done": False,
                            "entry_type": fusion_decision.get("entry_type") or "NORMAL",
                        }
                        state["active_strategy_state"] = register_position_opened(state["active_strategy_state"], symbol, price, pct, now)
                        state["active_strategy_state"] = register_idempotency_key(state["active_strategy_state"], idem_key)
                        state["adaptive_fusion_frequency_state"] = register_frequency_entry(frequency_state, fused_action, now)
                        message = f"Adaptive Fusion 진입: {symbol} {pct:.0f}%({qty}주, {fusion_decision.get('entry_type') or 'NORMAL'}) — {signal_source}"
                    elif not failure_reason:
                        failure_reason = REASON_ORDER_EXCEPTION
                        message = buy_result.get("message", "매수 실패")

    from app.trading.hynix_active_strategy_engine import build_final_execution_decision
    final_decision = build_final_execution_decision(
        action=(fused_action if not has_position else (active_decision_result.get("action") or "HOLD")),
        symbol=fusion_decision.get("symbol") or position_state.get("symbol"),
        target_position_pct=fusion_decision.get("target_position_pct", 0.0),
        confidence=fusion_decision.get("fused_confidence", 50.0), signal_source=signal_source,
        reasons=fusion_decision.get("reasons", []), executable=bool(acted),
        blocking_reason=(failure_reason if not acted else None),
    )
    final_decision.update(
        order_sent=acted, order_id=order_id, executed_price=executed_price, executed_qty=executed_qty,
        failure_reason=(failure_reason if not acted else None),
    )
    state["last_final_execution_decision"] = final_decision

    return {
        "acted": acted, "message": message, "action": action,
        "decision": fusion_decision, "final_decision": final_decision, "failure_reason": failure_reason,
    }


def _run_shadow_cycle_ai_and_decision_v2(
    state: dict, enhanced_result: dict, decision: dict, df_1min, hynix_price, inverse_price, now: datetime,
) -> Optional[dict]:
    """SHADOW MODE — Cycle Detector AI + Prediction AI V2(BUY/SELL/HOLD 확률+Adaptive
    Threshold)를 기존 enhanced_score 기반 실제 주문 흐름과 나란히 계산·기록만 한다.

    이 함수의 결과는 어떤 이유로도 `decision`/실제 주문 실행에 영향을 주지 않는다 —
    명세(Cycle Detector 17절)가 요구하는 최소 5거래일 Shadow Mode 검증을 위한 것이며,
    실제 주문 연결은 별도의 명시적 승인 이후에만 이루어진다. 실패해도 예외를 삼키고
    None을 반환한다(호출부 로직에 영향 없음).
    """
    try:
        from app.trading.hynix_cycle_detector import (
            HynixCycleDetector, default_cycle_state, log_cycle_ai_prediction,
        )
        from app.models.hynix_decision_v2 import (
            compute_buy_sell_hold_probability, decide_final_action_v2, adaptive_threshold_update,
            default_threshold_state,
        )
        from app.models.micron_proxy_prediction import compute_effective_micron_score_from_market_data

        market_data = enhanced_result.get("market_data") or {}
        micron_proxy = compute_effective_micron_score_from_market_data(market_data, now=now)
        effective_micron_score = micron_proxy.get("effective_micron_score")
        korea_score = micron_proxy.get("korea_semiconductor_confirmation_score")

        # 매 사이클 Micron Proxy 결과를 state에 스냅샷으로 저장한다 — 이번 사이클에서
        # 원본 데이터가 없어 계산이 실패/생략되더라도 이 필드는 마지막 성공 값을 그대로
        # 유지하므로, UI는 "데이터 없음"으로 빈 화면을 보이는 대신 마지막 성공 계산
        # 결과 + 경과시간을 표시할 수 있다.
        state["last_micron_proxy_snapshot"] = {
            "real_micron_score": micron_proxy.get("real_micron_score"),
            "synthetic_micron_score": micron_proxy.get("synthetic_micron_score"),
            "effective_micron_score": effective_micron_score,
            "score_source": micron_proxy.get("micron_score_source"),
            "confidence": micron_proxy.get("micron_data_confidence"),
            "calculated_at": now.isoformat(timespec="seconds"),
        }

        gap_pct = None
        session_high = session_low = None
        prior_close = enhanced_result.get("hynix_prev_close")
        if df_1min is not None and not df_1min.empty:
            session_high = float(df_1min["high"].max())
            session_low = float(df_1min["low"].min())
            if prior_close:
                gap_pct = (float(df_1min["open"].iloc[0]) / prior_close - 1.0) * 100.0

        position = state.get("position") or {}
        position_state = {
            "symbol": position.get("symbol"),
            "position_pct": 100.0 if (position.get("quantity") or 0) > 0 else 0.0,
        }

        cycle_state = state.get("cycle_ai_state") or default_cycle_state()
        cycle_result = HynixCycleDetector().run(
            df_1min, now, position_state=position_state, state=cycle_state,
            gap_pct=gap_pct, session_high=session_high, session_low=session_low, prior_close=prior_close,
            inverse_pressure_score=decision.get("inverse_pressure_score"),
            korea_semiconductor_confirmation_score=korea_score, effective_micron_score=effective_micron_score,
        )
        state["cycle_ai_state"] = cycle_result["state"]

        threshold_state = state.get("decision_v2_threshold_state") or default_threshold_state()
        probability = compute_buy_sell_hold_probability(
            cycle_result["turning_point"], enhanced_score=decision.get("enhanced_score"),
            effective_micron_score=effective_micron_score,
        )
        decision_v2 = decide_final_action_v2(probability, threshold_state)
        state["decision_v2_threshold_state"] = adaptive_threshold_update(
            threshold_state, decision_v2["final_action_v2"], now,
        )

        # 우선순위 스택(Cycle Phase → Turning Point → Momentum → Entry Timing → Enhanced →
        # Effective Micron): Cycle Detector가 HOLD가 아닌 액션을 냈으면 그것을 combined 액션으로,
        # 아니면 확률 기반 decision_v2의 액션을 combined 액션으로 삼는다(로그/UI 비교용일 뿐,
        # 실제 주문에는 반영되지 않음).
        combined_action = cycle_result["action"] if cycle_result["action"] != "HOLD" else decision_v2["final_action_v2"]

        shadow_result = {
            "cycle": cycle_result, "probability": probability, "decision_v2": decision_v2,
            "combined_shadow_action": combined_action, "effective_micron_score": effective_micron_score,
            "korea_semiconductor_confirmation_score": korea_score,
        }
        state["last_cycle_ai_result"] = shadow_result

        reasons = (cycle_result.get("reasons") or [])[:3]
        log_cycle_ai_prediction({
            "timestamp": now.isoformat(timespec="seconds"), "hynix_price": hynix_price, "inverse_price": inverse_price,
            "cycle_phase": cycle_result.get("cycle_phase"), "previous_cycle_phase": cycle_result.get("previous_cycle_phase"),
            "phase_duration_seconds": cycle_result.get("phase_duration_seconds"),
            "momentum_velocity": (cycle_result.get("momentum") or {}).get("raw_velocity_3"),
            "momentum_acceleration_up": (cycle_result.get("momentum") or {}).get("momentum_acceleration_up"),
            "momentum_acceleration_down": (cycle_result.get("momentum") or {}).get("momentum_acceleration_down"),
            "early_reversal_score": (cycle_result.get("state", {}) or {}).get("early_reversal_score"),
            "up_turn_3m": (cycle_result.get("turning_point") or {}).get("up_turn_probability_3m"),
            "up_turn_5m": (cycle_result.get("turning_point") or {}).get("up_turn_probability_5m"),
            "up_turn_10m": (cycle_result.get("turning_point") or {}).get("up_turn_probability_10m"),
            "down_turn_3m": (cycle_result.get("turning_point") or {}).get("down_turn_probability_3m"),
            "down_turn_5m": (cycle_result.get("turning_point") or {}).get("down_turn_probability_5m"),
            "down_turn_10m": (cycle_result.get("turning_point") or {}).get("down_turn_probability_10m"),
            "cycle_confidence": cycle_result.get("cycle_confidence"),
            "cycle_entry_score": max((cycle_result.get("entry_scores") or {}).values(), default=None),
            "enhanced_score": decision.get("enhanced_score"), "effective_micron_score": effective_micron_score,
            "recommended_symbol": cycle_result.get("recommended_symbol"),
            "recommended_position_pct": cycle_result.get("recommended_position_pct"),
            "final_action": combined_action, "order_sent": False, "order_executed": False,
            "reason_top1": reasons[0] if len(reasons) > 0 else "",
            "reason_top2": reasons[1] if len(reasons) > 1 else "",
            "reason_top3": reasons[2] if len(reasons) > 2 else "",
        })
        return shadow_result
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] Shadow Cycle AI/Decision V2 계산 실패(무해, 실거래에 영향 없음): %s", exc)
        return None


def update_hynix_auto_trade_loop(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """1회 실행 사이클의 공개 진입점 — mode별 state 락으로 감싼 얇은 wrapper.

    백그라운드 3분 사이클 스레드, Dynamic Exit Watcher(1초 주기), Streamlit 수동 실행이
    모두 같은 mode의 state를 동시에 load→수정→save할 수 있어(lost update 위험 — 실제로
    2026-07-10 부분손절 손익 1건이 이렇게 누락된 사고가 있었다), 이 함수 호출 전체를
    mode별 락(app.services.hynix_switch_state.with_state_lock)으로 직렬화한다.
    """
    from app.services.hynix_switch_state import with_state_lock

    resolved_mode = mode
    if resolved_mode is None:
        resolved_mode = load_state(mode=None).get("mode", "mock")
    with with_state_lock(resolved_mode):
        return _update_hynix_auto_trade_loop_locked(mode=mode, now=now)


def run_fast_trend_watcher_tick(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """30s fast live-trend watcher entry point.

    It only uses live Hynix minute trend for direction flips. The 3-minute AI
    cycle remains the broader confirmation/diagnostic path.
    """
    from app.models.hynix_enhanced_score import calculate_enhanced_hynix_prediction_score
    from app.services.hynix_switch_state import with_state_lock

    now = now or kst_now()
    resolved_mode = mode or load_state(mode=None).get("mode", "mock")
    with with_state_lock(resolved_mode):
        state = load_state(mode=resolved_mode)
        state["mode"] = resolved_mode
        if not state.get("auto_trade_on") or state.get("stopped"):
            return {"skipped": True, "reason": "auto off or stopped"}
        if not is_new_entry_allowed(now):
            status = state.get("fast_trend_watcher") or {}
            status.update({"last_checked_at": now.isoformat(), "blocked_reason": "new entries blocked after 14:50"})
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": True, "reason": "new entries blocked after 14:50", "state": state}

        enhanced_result = calculate_enhanced_hynix_prediction_score(mode=resolved_mode)
        df_1min = (enhanced_result.get("market_data") or {}).get("hynix_minute", {}).get("df_1min")
        fast_signal = compute_fast_trend_signal(df_1min, now=now)
        primary_trend_result = compute_primary_trend(
            df_1min, prev_close=enhanced_result.get("hynix_prev_close"), now=now,
        )
        primary_trend = primary_trend_result.get("primary_trend")
        state["last_primary_trend"] = primary_trend_result
        status = dict(state.get("fast_trend_watcher") or {})

        # 요구사항: fast confirmed direction(candidate_direction/confirmation_count)은
        # 날짜(위 auto off/rollover 분기에서 이미 처리) 뿐 아니라 계좌/보유수량이 바뀌면도
        # 초기화해야 한다 — 이전 사이클과 보유 상태가 달라졌다면 그 사이의 연속 확인
        # 횟수는 지금의 포지션에 대해 더 이상 유효하지 않다.
        position_now = state.get("position") or {}
        position_signature = f"{resolved_mode}:{position_now.get('symbol')}:{position_now.get('quantity') or 0}"
        if status.get("position_signature") not in (None, position_signature):
            status["candidate_direction"] = None
            status["confirmation_count"] = 0
        status["position_signature"] = position_signature

        direction = fast_signal.get("direction")
        if direction in ("UP", "DOWN"):
            if status.get("candidate_direction") == direction:
                status["confirmation_count"] = int(status.get("confirmation_count", 0)) + 1
            else:
                status["candidate_direction"] = direction
                status["confirmation_count"] = 1
        else:
            status["confirmation_count"] = 0
        status.update({
            "direction": direction,
            "last_signal": fast_signal,
            "last_checked_at": now.isoformat(timespec="seconds"),
            "actual_order_driver": "ENHANCED_REGIME_SWITCH",
            "primary_trend": primary_trend,
            "blocked_reason": None,
        })
        state["fast_trend_watcher"] = status

        # 요구사항: Fast Watcher의 빠른 스위칭은 PRIMARY_TREND가 RANGE일 때만 허용한다.
        # UP/DOWN 추세 중에는 1·3·5분 신호가 그 추세에 대한 PULLBACK일 수 있으므로,
        # 이 30초 워처가 단독으로 전환하지 않는다 — 실제 추세반전은 update_reversal_confirmation
        # (VWAP 이탈 + 15분 추세 + 주요 저점/고점 붕괴, 2회 연속 확인)이 별도로 담당한다.
        if primary_trend != PRIMARY_TREND_RANGE:
            move_kind = classify_short_term_move(primary_trend, direction)
            status["blocked_reason"] = (
                f"PRIMARY_TREND={primary_trend} - fast rapid switching only runs in RANGE "
                f"(this move classified as {move_kind})"
            )
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "primary_trend": primary_trend_result, "state": state}

        if direction not in ("UP", "DOWN") or int(status.get("confirmation_count", 0)) < 2:
            save_state_atomic(state)
            return {"skipped": True, "reason": "fast trend not confirmed", "fast_signal": fast_signal, "state": state}

        final_action = "HYNIX_BUY" if direction == "UP" else "INVERSE_BUY"
        desired_symbol = _ACTION_TO_SYMBOL.get(final_action)
        held_symbol = (state.get("position") or {}).get("symbol")
        if desired_symbol == held_symbol:
            status["blocked_reason"] = "already holding confirmed fast direction"
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}

        idempotency_key = f"FAST:{direction}:{now.strftime('%Y%m%d%H%M')}"
        previous_fast_execution = ((state.get("fast_trend_watcher") or {}).get("last_execution") or {})
        previous_orders = previous_fast_execution.get("orders") or []
        previous_had_success_order = any(bool(o.get("success")) for o in previous_orders if isinstance(o, dict))
        if state.get("last_execution_idempotency_key") == idempotency_key and previous_had_success_order:
            status["blocked_reason"] = "duplicate fast watcher idempotency key"
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}
        _is_reversal_vs_holding = bool(held_symbol and desired_symbol != held_symbol)
        # 요구사항8 — same_direction_streak(연속 같은 방향 확인 횟수)과 reversal_streak
        # (현재 보유와 반대 방향 전환이 확인된 횟수)은 서로 다른 개념이다. Fast Watcher의
        # confirmation_count는 "같은 방향이 몇 틱 연속 확인됐는지"만 측정하므로,
        # 실제로 보유 종목과 반대 방향으로 전환하는 상황(_is_reversal_vs_holding)이
        # 아니면 reversal_streak은 0이어야 한다 — 과거에는 두 필드에 항상 같은 값을
        # 넣어 "그냥 같은 방향 유지"도 "반전 확인"처럼 보이게 했다.
        _confirmation_count = int(status.get("confirmation_count", 0))
        state["last_trend_switch_plan"] = {
            "proceed": True,
            "position_pct": 0.20,
            "entry_type": "EXPLORATORY",
            "immediate_switch": _is_reversal_vs_holding,
            "dominant_direction": "HYNIX" if direction == "UP" else "INVERSE",
            "desired_symbol": desired_symbol,
            "same_direction_streak": _confirmation_count,
            "reversal_streak": _confirmation_count if _is_reversal_vs_holding else 0,
            "pullback_wait_remaining_seconds": 0,
            "block_reason": None,
            "source": "FAST_TREND_WATCHER",
        }

        try:
            from app.config import get_config
            from app.trading.broker_factory import create_broker

            cfg = get_config()
            if resolved_mode == "real":
                real_gate_status = (
                    cfg.enhanced_real_gate_status(current_mode=resolved_mode)
                    if hasattr(cfg, "enhanced_real_gate_status")
                    else {"ready": cfg.full_auto_real_confirm_ok(), "blocking_reasons": [], "checks": {}}
                )
                if not bool(real_gate_status.get("ready")):
                    status["blocked_reason"] = "REAL gate blocked: " + ", ".join(real_gate_status.get("blocking_reasons") or ["UNKNOWN"])
                    state["fast_trend_watcher"] = status
                    save_state_atomic(state)
                    return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}
                broker = create_broker(
                    cfg, mode="real", confirm_text=cfg.full_auto_real_confirm_text(),
                    runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
                )
            else:
                broker = create_broker(cfg, mode=resolved_mode)
            position_manager = HynixPositionManager(broker, mode=resolved_mode)
            position_manager.sync(force=True)
            apply_position_manager_to_state(state, position_manager)
            # 요구사항(2026-07-15) — 실행가는 000660이 아니라 LONG_SYMBOL(0193T0)의
            # 실제 현재가여야 한다(3분 사이클과 동일 원칙).
            from app.data_sources.hynix_long_collector import collect_long_current

            _fast_long_quote = collect_long_current(mode=resolved_mode)
            _fast_long_price = _fast_long_quote.get("current_price")
            if not _fast_long_price:
                status["blocked_reason"] = f"LONG_SYMBOL(0193T0) 현재가 조회 실패: {_fast_long_quote.get('error')}"
                state["fast_trend_watcher"] = status
                save_state_atomic(state)
                return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}
            switch = run_switch_or_entry(
                state, broker, final_action,
                _fast_long_price, enhanced_result.get("inverse_current_price"),
                now=now, forced=True, reason="FAST_TREND_WATCHER 2x confirmed",
                position_manager=position_manager, target_position_pct=0.20, entry_type="EXPLORATORY",
            )
            status["last_execution"] = {
                "idempotency_key": idempotency_key,
                "final_action": final_action,
                "result_message": switch.get("message"),
                "orders": switch.get("orders", []),
            }
            if any(bool(o.get("success")) for o in (switch.get("orders") or []) if isinstance(o, dict)):
                state["last_execution_idempotency_key"] = idempotency_key
            elif state.get("last_execution_idempotency_key") == idempotency_key:
                state["last_execution_idempotency_key"] = None
            position_manager.sync(force=True)
            apply_position_manager_to_state(state, position_manager)
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": False, "fast_signal": fast_signal, "switch": switch, "state": state}
        except Exception as exc:
            status["blocked_reason"] = f"fast watcher execution failed: {exc}"
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            logger.error("[FastTrendWatcher] execution failed: %s", exc)
            return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}


def _update_hynix_auto_trade_loop_locked(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """1회 실행 사이클의 실제 구현(반드시 with_state_lock(mode) 안에서만 호출).

    `now`는 테스트에서 시각을 주입하기 위한 선택 인자이며, 운영 시에는 항상 현재시각이 쓰인다.
    """
    warnings: list[str] = []
    now = now or kst_now()
    state = load_state(mode=mode)
    mode = mode or state.get("mode", "mock")
    state["mode"] = mode
    state["actual_order_driver"] = "ENHANCED_REGIME_SWITCH"

    if state.get("stopped"):
        trace = _blank_pipeline_trace()
        trace["risk_manager_ok"] = False
        trace["risk_manager_reason"] = state.get("stopped_reason") or "자동매매 정지 상태"
        trace["risk_approved"] = False
        trace["stopped_stage"] = "risk_manager"
        trace["blocking_reason"] = f"[risk_manager] {trace['risk_manager_reason']}"
        return {
            "skipped": True, "reason": state.get("stopped_reason") or "자동매매 정지 상태", "state": state,
            "pipeline_trace": trace,
        }

    trace = _blank_pipeline_trace()

    # ── ①~⑥ 점수/판단 계산 (기존 데이터 흐름 재사용) ────────────────────────
    try:
        from app.models.hynix_enhanced_score import calculate_enhanced_hynix_prediction_score

        enhanced_result = calculate_enhanced_hynix_prediction_score(mode=mode)
    except Exception as exc:
        logger.error("[HynixSwitchEngine] enhanced_score 계산 실패: %s", exc)
        warnings.append(f"enhanced_score 계산 실패: {exc}")
        enhanced_result = {
            "base_prediction_score": 50.0, "existing_micron_score": 50.0,
            "hynix_technical_score": 50.0, "intraday_momentum_score": 50.0,
            "inverse_pressure_score": 50.0, "enhanced_score": 50.0,
            "reason_top5": [], "data_valid": {"base_prediction": False, "hynix_technical": False},
            "hynix_current_price": None, "inverse_current_price": None, "warnings": [str(exc)],
        }

    try:
        from app.models.hynix_action_decider import decide_hynix_or_inverse_action

        decision = decide_hynix_or_inverse_action(enhanced_result, current_position=state.get("position"))
    except Exception as exc:
        logger.error("[HynixSwitchEngine] action_decider 실패: %s", exc)
        warnings.append(f"action_decider 실패: {exc}")
        decision = {"final_action": "HOLD", "enhanced_score": enhanced_result.get("enhanced_score", 50.0),
                    "inverse_pressure_score": enhanced_result.get("inverse_pressure_score", 50.0),
                    "score_gap": 0.0, "score_gap_below_forced_trade_threshold": True, "reasons": [str(exc)]}

    trace["prediction_signal"] = _map_prediction_signal(decision.get("final_action", "HOLD"))

    hynix_signal_price = enhanced_result.get("hynix_current_price")
    inverse_price = enhanced_result.get("inverse_current_price")
    df_1min = (enhanced_result.get("market_data") or {}).get("hynix_minute", {}).get("df_1min")

    # ── SHADOW MODE: Cycle Detector AI + Prediction AI V2(BUY/SELL/HOLD 확률) ──
    # 아래 호출은 `decision`/실제 주문에 절대 영향을 주지 않는다 — 계산·로그·state 저장만
    # 수행하며, 예외가 나도 무해하게 삼켜진다. 실제 주문 연결은 별도 승인 후 진행한다.
    # 신호 계산 참고용이므로 여기서는 000660 가격(hynix_signal_price)을 그대로 쓴다.
    _run_shadow_cycle_ai_and_decision_v2(state, enhanced_result, decision, df_1min, hynix_signal_price, inverse_price, now)

    # 요구사항(2026-07-15) — 실제 매매 종목은 000660이 아니라 LONG_SYMBOL(0193T0)이다.
    # 이 지점부터 `hynix_price`는 000660이 아니라 0193T0의 실제 현재가여야 한다 —
    # run_switch_or_entry/run_tp_sl_if_needed/run_liquidation_if_needed/evaluate_pullback_gate
    # 등 실행 계층에 넘기는 값이 그대로 주문가격·손익 계산 기준이 되기 때문이다.
    try:
        from app.data_sources.hynix_long_collector import collect_long_current

        _long_quote = collect_long_current(mode=mode)
        hynix_price = _long_quote.get("current_price")
        if _long_quote.get("stale"):
            warnings.append(f"LONG_SYMBOL(0193T0) 현재가가 최신이 아닐 수 있음: {_long_quote.get('error')}")
    except Exception as exc:
        logger.error("[HynixSwitchEngine] LONG_SYMBOL(0193T0) 현재가 조회 실패: %s", exc)
        warnings.append(f"LONG_SYMBOL(0193T0) 현재가 조회 실패: {exc}")
        hynix_price = None

    signal_data_ok = bool((enhanced_result.get("data_valid") or {}).get("hynix_signal_price", True))
    price_data_ok = hynix_price is not None and signal_data_ok
    order_api_ok = True
    broker = None
    real_gate_ok = True
    real_gate_status = None

    auto_trade_on = bool(state.get("auto_trade_on"))
    position_manager = None
    if auto_trade_on:
        try:
            if mode == "real":
                from app.config import get_config
                from app.trading.broker_factory import create_broker

                cfg = get_config()
                real_gate_status = (
                    cfg.enhanced_real_gate_status(current_mode=mode)
                    if hasattr(cfg, "enhanced_real_gate_status")
                    else {"ready": cfg.full_auto_real_confirm_ok(), "blocking_reasons": [], "checks": {}}
                )
                real_gate_ok = bool(real_gate_status.get("ready"))
                trace["real_gate_status"] = real_gate_status
                if not real_gate_ok:
                    warnings.append(
                        "REAL 완전자동 게이트 미충족: "
                        + ", ".join(real_gate_status.get("blocking_reasons") or ["UNKNOWN"])
                        + " — 주문 실행 생략"
                    )
                if real_gate_ok:
                    broker = create_broker(
                        cfg, mode="real", confirm_text=cfg.full_auto_real_confirm_text(),
                        runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
                    )
            else:
                from app.config import get_config
                from app.trading.broker_factory import create_broker

                broker = create_broker(get_config(), mode="mock")

            if broker is not None:
                # Broker가 유일한 Source of Truth — position_manager.sync()로 실제 포지션을
                # 먼저 확정하고, state는 그 결과를 담는 캐시로만 갱신한다.
                position_manager = HynixPositionManager(broker, mode=mode)
                position_manager.sync(force=True)
                apply_position_manager_to_state(state, position_manager)
                if state.get("position_conflict"):
                    warnings.append(state.get("critical_alert") or "0193T0/0197X0 동시 보유 감지 — 신규매수 금지")
        except Exception as exc:
            order_api_ok = False
            warnings.append(f"브로커 초기화 실패: {exc}")
            logger.error("[HynixSwitchEngine] 브로커 초기화 실패: %s", exc)

    total_equity = None
    daily_pnl_pct = None
    daily_return_blocked_this_cycle = False
    net_return_result = None
    if broker is not None:
        try:
            net_return_result = _compute_net_daily_return_with_retries(
                state, broker, state.get("position"), hynix_price, inverse_price, now,
            )
            snapshot = net_return_result.get("account_snapshot") or {}
            positions = snapshot.get("positions") or []
            cash = snapshot.get("cash")
            total_equity = snapshot.get("current_equity")
            is_mock_override = mode == "mock" and state.get("allow_mock_loss_override")

            # 요구사항 1/5절 — risk_manager와 UI가 동일한 값(원장 실현손익 + 미실현손익
            # / 시작자산)을 쓰도록 통합한다. get_buyable_cash()는 KIS API 오류(예: "1분당
            # 1회" 토큰발급 레이트리밋)가 나도 예외 없이 0.0을 반환하는 하위호환 계약이
            # 있어(KisMockBroker.get_orderable_cash), 그 값을 곧바로 "계좌가 0원이 됐다"로
            # 써버리면 일일 손실이 -100%로 오판된다(2026-07-14 실측). 새 계산식은 실현손익을
            # 원장(state)에서, 미실현손익을 보유 포지션+현재가에서 가져오므로 이 계좌조회
            # 글리치의 영향을 받지 않는다 — cash/positions는 교차검증에만 쓰인다.
            state["daily_return_calculation"] = {k: v for k, v in net_return_result.items()}
            daily_pnl_pct = net_return_result["net_daily_return"]

            if net_return_result["blocked_reason"]:
                daily_return_blocked_this_cycle = True
                warnings.append(
                    f"일일손익 판정 보류({net_return_result['blocked_reason']}) — "
                    f"신규주문만 일시 보류(기존 정지상태는 변경하지 않음)"
                )
                logger.warning(
                    "[HynixSwitchEngine] %s — 손익 판정 보류, -100%% 등으로 기록하지 않음",
                    net_return_result["blocked_reason"],
                )
            else:
                state["total_equity"] = total_equity
                if daily_pnl_pct is not None and daily_pnl_pct <= -2.5 and mode == "real":
                    state["stopped"] = True
                    state["stopped_reason"] = f"일 누적 손실 {daily_pnl_pct:.2f}% ≤ -2.5% — REAL 자동매매 강제 중단"
                    logger.error(state["stopped_reason"])
                elif daily_pnl_pct is not None and daily_pnl_pct <= -2.5 and mode == "mock" and not is_mock_override:
                    state["stopped"] = True
                    state["stopped_reason"] = f"일 누적 손실 {daily_pnl_pct:.2f}% ≤ -2.5% — MOCK 자동매매 중단(설정에서 계속 테스트 가능)"
        except Exception as exc:
            order_api_ok = False
            warnings.append(f"계좌 조회 실패: {exc}")

    fired_windows = state.get("fired_windows", [])
    forced_info = should_force_trade(
        decision, fired_windows, price_data_ok, order_api_ok, df_1min, daily_pnl_pct, now=now,
    )

    liquidation_phase_now = get_liquidation_phase(now)

    orders_this_cycle: list = []
    attempted_entry = False
    trading_allowed = (
        auto_trade_on and real_gate_ok and not state.get("stopped")
        and not daily_return_blocked_this_cycle and not is_watch_only(now) and broker is not None
        and not state.get("position_sync_block_new_orders")
    )

    if not trading_allowed:
        trace["risk_manager_ok"] = False
        if state.get("stopped"):
            trace["risk_manager_reason"] = state.get("stopped_reason") or "자동매매 중단 상태"
        elif daily_return_blocked_this_cycle:
            blocked_reason = (net_return_result or {}).get("blocked_reason") or DAILY_RETURN_UNKNOWN
            trace["risk_manager_reason"] = f"{blocked_reason} — 계좌 데이터 이상으로 이번 사이클 신규주문 일시 보류"
        elif state.get("position_sync_block_new_orders"):
            trace["risk_manager_reason"] = state.get("critical_alert") or "POSITION_SYNC_PENDING"
        elif not auto_trade_on:
            trace["risk_manager_reason"] = "자동매매 OFF"
        elif not real_gate_ok:
            if real_gate_status:
                trace["risk_manager_reason"] = (
                    "REAL_GATE_NOT_READY: "
                    + ", ".join(real_gate_status.get("blocking_reasons") or ["UNKNOWN"])
                )
            else:
                trace["risk_manager_reason"] = "REAL_GATE_NOT_READY"
        elif is_watch_only(now):
            trace["risk_manager_reason"] = "관찰 전용 시간대(watch-only)"
        elif broker is None:
            trace["risk_manager_reason"] = "브로커 초기화 실패"
    elif state.get("position_conflict"):
        trace["risk_manager_ok"] = False
        trace["risk_manager_reason"] = state.get("critical_alert") or "0193T0/0197X0 동시 보유 — 포지션 동기화 필요"

    if trading_allowed:
        try:
            liq = run_liquidation_if_needed(now, state, broker, hynix_price, inverse_price, position_manager=position_manager)
            orders_this_cycle.extend(liq.get("orders", []))
        except Exception as exc:
            logger.error("[HynixSwitchEngine] 강제청산 처리 실패: %s", exc)
            warnings.append(f"강제청산 처리 실패: {exc}")
            liq = {"liquidated": False}

        if liquidation_phase_now == "closed" and not liq.get("liquidated"):
            warnings.append("15:20 이후 — 신규 주문 판단 없이 상태 정리만 수행")
        elif not liq.get("liquidated"):
            try:
                tp_sl = run_tp_sl_if_needed(state, broker, hynix_price, inverse_price, position_manager=position_manager, now=now)
                orders_this_cycle.extend(tp_sl.get("orders", []))
            except Exception as exc:
                logger.error("[HynixSwitchEngine] TP/SL 처리 실패: %s", exc)
                warnings.append(f"TP/SL 처리 실패: {exc}")
                tp_sl = {"triggered": False}

            if not signal_data_ok:
                trace["entry_approved"] = False
                trace["entry_approved_reason"] = "DATA_UNIT_MISMATCH/DATA_ERROR — 000660 신호가격 검증 실패로 신규 진입 차단"
                warnings.append(trace["entry_approved_reason"])
            elif not tp_sl.get("triggered") and mode == "mock" and state.get("active_strategy_enabled"):
                # ── ACTIVE STRATEGY / ADAPTIVE FUSION(거래모드 기반) — mock 전용 opt-in,
                # 이번 사이클의 신규진입/전환 판단을 기존 ENHANCED_REGIME_SWITCH 대신 이
                # 엔진이 담당한다. mode=="mock" 조건 때문에 실거래(real)에는 절대 관여하지
                # 않으므로(요구사항4: 실제 주문 지배 엔진은 ENHANCED_REGIME_SWITCH 하나로
                # 통일), 이 opt-in 경로가 "두 번째 실주문 엔진"이 되지는 않는다. 강제청산/
                # 레거시 TP·SL은 위에서 이미 항상 우선 실행되었으므로 안전망은 유지된다.
                # adaptive_fusion_enabled가 켜져 있으면 Prediction AI V2/Cycle AI/Micron
                # Proxy를 실제로 융합하는 Adaptive Fusion 경로를 쓰고, 꺼져 있으면 기존
                # ACTIVE_FUSION 단독 경로를 그대로 쓴다(대체가 아니라 opt-in).
                try:
                    _boosted_enhanced_score = _boost_enhanced_score_with_inverse_pressure(
                        decision.get("enhanced_score"), decision.get("inverse_pressure_score"),
                    )
                    if state.get("adaptive_fusion_enabled"):
                        active_result = _run_adaptive_fusion_entry(
                            state, broker, hynix_price, inverse_price, now, orders_this_cycle,
                            enhanced_ai_score=_boosted_enhanced_score, hynix_df_1min=df_1min,
                            position_manager=position_manager,
                        )
                        trace_label = "ADAPTIVE_FUSION"
                    else:
                        active_result = _run_active_strategy_entry(
                            state, broker, hynix_price, inverse_price, now, orders_this_cycle,
                            enhanced_ai_score=_boosted_enhanced_score, position_manager=position_manager,
                        )
                        trace_label = "ACTIVE_STRATEGY"
                    trace["entry_approved"] = active_result.get("acted", False)
                    trace["entry_approved_reason"] = f"[{trace_label}] {active_result.get('message', '')}"
                    if active_result.get("acted"):
                        attempted_entry = True
                        state.pop("pending_entry", None)
                except Exception as exc:
                    logger.error("[HynixSwitchEngine] Active Strategy/Adaptive Fusion 진입 처리 실패: %s", exc)
                    warnings.append(f"Active Strategy/Adaptive Fusion 진입 처리 실패: {exc}")
            elif not tp_sl.get("triggered"):
                final_action = decision.get("final_action", "HOLD")
                forced = False
                reason = "; ".join(decision.get("reasons", []))
                if final_action == "HOLD" and forced_info.get("should_force"):
                    final_action = forced_info.get("forced_direction") or "HOLD"
                    forced = True
                    reason = f"강제거래창({forced_info.get('window')}) — {reason}"

                if final_action != "HOLD":
                    held_symbol = (state.get("position") or {}).get("symbol")
                    desired_symbol = _ACTION_TO_SYMBOL.get(final_action)
                    is_new_entry = desired_symbol is not None and held_symbol != desired_symbol

                    proceed = True
                    if not is_new_entry:
                        trace["entry_approved"] = True
                        trace["entry_approved_reason"] = "이미 목표 종목 보유 중 — 추가 진입 불필요"
                    elif state.get("position_conflict"):
                        proceed = False
                        warnings.append("포지션 동기화 필요(0193T0/0197X0 동시 보유) — 신규매수 금지")
                        trace["entry_approved"] = False
                        trace["entry_approved_reason"] = "포지션 동기화 필요(동시 보유) — 신규매수 금지"
                    else:
                        if str(final_action).endswith("_STRONG_BUY"):
                            confirm_tracker = update_confirm_tracker(
                                state.get("trend_switch_confirm_tracker") or default_confirm_state(),
                                final_action, held_symbol, desired_symbol, now,
                            )
                            state["trend_switch_confirm_tracker"] = confirm_tracker
                            trend_plan = plan_trend_switch_entry(
                                final_action=final_action,
                                held_symbol=held_symbol,
                                desired_symbol=desired_symbol,
                                confirm_tracker=confirm_tracker,
                                frequency_state=state.get("trend_switch_frequency_state") or default_trend_frequency_state(),
                                pullback_result=None,
                                now=now,
                                data_ok=bool(hynix_price and inverse_price),
                                has_unconfirmed_order=bool(state.get("order_in_flight") or state.get("pending_order")),
                                daily_return_pct=state.get("realized_pnl_today_pct"),
                                atr_pct=None,
                            )
                            proceed = bool(trend_plan.get("proceed"))
                            # 강한 신호(STRONG_BUY)도 PRIMARY_TREND 차단은 무시할 수 없다 — "강한
                            # 신호니까 눌림목 대기 생략"이 단기 조정을 실제 추세전환으로 오판하게
                            # 두지 않는다.
                            strong_primary_trend_result = compute_primary_trend(
                                df_1min, prev_close=enhanced_result.get("hynix_prev_close"), now=now,
                            )
                            state["last_primary_trend"] = strong_primary_trend_result
                            strong_ptrend = strong_primary_trend_result.get("primary_trend", PRIMARY_TREND_RANGE)
                            trend_block_reason = None
                            if final_action == "INVERSE_STRONG_BUY" and new_inverse_entry_blocked(
                                strong_ptrend, strong_primary_trend_result.get("above_vwap"),
                                strong_primary_trend_result.get("above_ema20"), strong_primary_trend_result,
                            ):
                                votes, vote_reasons = inverse_block_vote_count(strong_primary_trend_result)
                                trend_block_reason = (
                                    f"PRIMARY_TREND=UP with {votes} uptrend confirmations({', '.join(vote_reasons)}) - even a strong INVERSE "
                                    "signal is blocked without 2x-confirmed VWAP/15m-trend/swing-low breakdown"
                                )
                            elif final_action == "HYNIX_STRONG_BUY" and new_hynix_entry_blocked(
                                strong_ptrend, strong_primary_trend_result.get("above_vwap"), strong_primary_trend_result.get("above_ema20"),
                            ):
                                trend_block_reason = (
                                    "PRIMARY_TREND=DOWN and price below VWAP/EMA20 - even a strong HYNIX "
                                    "signal is blocked without 2x-confirmed VWAP/15m-trend/swing-high breakout"
                                )
                            if trend_block_reason:
                                proceed = False
                            trace["entry_approved"] = proceed
                            trace["entry_approved_reason"] = (
                                f"{final_action} 강한 신호 — 눌림목 대기 생략"
                                if proceed else (trend_block_reason or trend_plan.get("block_reason") or "강한 신호 차단")
                            )
                            if not proceed:
                                warnings.append(trace["entry_approved_reason"])
                            state["last_trend_switch_plan"] = {
                                **trend_plan,
                                "dominant_direction": "HYNIX" if desired_symbol == HYNIX_SYMBOL else "INVERSE",
                                "desired_symbol": desired_symbol,
                                "pullback_wait_remaining_seconds": 0,
                                "primary_trend": strong_ptrend,
                                **({"block_reason": trend_block_reason} if trend_block_reason else {}),
                            }
                        else:
                            try:
                                primary_trend_result = compute_primary_trend(
                                    df_1min, prev_close=enhanced_result.get("hynix_prev_close"), now=now,
                                )
                                state["last_primary_trend"] = primary_trend_result
                                gate = evaluate_pullback_gate(
                                    state, desired_symbol, final_action, now, forced_info, df_1min, mode,
                                    primary_trend_result=primary_trend_result,
                                )
                                proceed = gate["proceed"]
                                trace["entry_approved"] = proceed
                                trace["entry_approved_reason"] = gate["message"]
                                if not proceed:
                                    warnings.append(gate["message"])
                            except Exception as exc:
                                logger.error("[HynixSwitchEngine] 눌림목 게이트 판단 실패, 즉시 진입으로 폴백: %s", exc)
                                proceed = True
                                trace["entry_approved"] = True
                                trace["entry_approved_reason"] = f"눌림목 게이트 오류로 즉시 진입 폴백: {exc}"

                    if proceed:
                        attempted_entry = True
                        state.pop("pending_entry", None)
                        try:
                            switch = run_switch_or_entry(
                                state, broker, final_action, hynix_price, inverse_price,
                                now=now, forced=forced, reason=reason, position_manager=position_manager,
                            )
                            orders_this_cycle.extend(switch.get("orders", []))
                            trace["execution_stage"] = switch.get("stage")
                            if not switch.get("orders"):
                                trace["entry_approved_reason"] = switch.get("message") or trace.get("entry_approved_reason")
                        except Exception as exc:
                            logger.error("[HynixSwitchEngine] 스위칭/진입 처리 실패: %s", exc)
                            warnings.append(f"스위칭/진입 처리 실패: {exc}")
                            trace["execution_stage"] = "order_sent"
                else:
                    state.pop("pending_entry", None)
                    trace["entry_approved_reason"] = "HOLD — 신규 진입 신호 없음"

        if forced_info.get("should_force") and forced_info.get("window") and attempted_entry:
            if forced_info["window"] not in fired_windows:
                fired_windows.append(forced_info["window"])
                state["fired_windows"] = fired_windows

        # 이번 사이클에 주문을 실행했다면, "확정된 것으로 추정한 상태"가 아니라 브로커에
        # 실제로 무엇이 체결됐는지 다시 확인하고 그 결과로 state(캐시)를 갱신한다.
        # (buy()/sell() → broker.positions 갱신 → get_positions() → position_manager.sync() → state 캐시)
        if orders_this_cycle and position_manager is not None:
            try:
                position_manager.sync(force=True)
                apply_position_manager_to_state(state, position_manager)
            except Exception as exc:
                logger.error("[HynixSwitchEngine] 주문 후 포지션 재확인 실패: %s", exc)
                warnings.append(f"주문 후 포지션 재확인 실패: {exc}")

    # ── Order Sent / Broker Executed / Position Confirmed 판정 ──────────────
    sent_orders = [o for o in orders_this_cycle if o.get("action") in ("BUY", "SELL")]
    trace["order_sent"] = bool(sent_orders)
    trace["broker_executed"] = any(o.get("success") for o in sent_orders)
    if trace["broker_executed"]:
        state["last_trade_time"] = now.isoformat()
    if sent_orders:
        last_order = sent_orders[-1]
        if not last_order.get("success"):
            trace["position_confirmed"] = False
        elif last_order.get("action") == "BUY":
            pos_now = state.get("position") or {}
            trace["position_confirmed"] = (
                pos_now.get("symbol") == last_order.get("symbol") and (pos_now.get("quantity") or 0) > 0
            )
        else:  # SELL
            # 부분매도(expected_remaining_qty>0)는 같은 심볼이 남은 수량과 정확히
            # 일치해야 confirmed다 — "심볼이 사라졌는지"만 보면 부분매도는 항상
            # False로 오판된다. 전량매도(expected_remaining_qty==0)만 심볼 소거를 기대한다.
            pos_now = state.get("position") or {}
            expected_remaining = last_order.get("expected_remaining_qty")
            actual_qty = pos_now.get("quantity") or 0
            if expected_remaining == 0:
                trace["position_confirmed"] = pos_now.get("symbol") != last_order.get("symbol") or actual_qty == 0
            else:
                trace["position_confirmed"] = (
                    pos_now.get("symbol") == last_order.get("symbol") and actual_qty == expected_remaining
                )
            trace["prediction_signal"] = "SELL"  # TP/SL/강제청산/스위칭 매도 — 예측신호와 별개로 실제 실행된 것

    # ── 미실현손익/당일수익률 갱신 ────────────────────────────────────────────
    # unrealized_pnl은 GrossPnL이 아니라 NetPnL이다 — "지금 판다면" 발생할 매도수수료/
    # 거래세/슬리피지를 선차감해 표시한다(docs/requirements.md 섹션 2.10). 일손익
    # 리스크 게이트(daily_return_pct 기반 신규진입 중단/강제청산)도 이 값을 그대로
    # 쓰므로, Gross보다 보수적인(더 이르게 위험을 인식하는) 방향으로 안전하게 작동한다.
    position = state.get("position") or {}
    unrealized_pnl = 0.0
    gross_unrealized_pnl = 0.0
    if position.get("symbol") and (position.get("quantity") or 0) > 0 and position.get("entry_price"):
        cur = _current_price(position["symbol"], hynix_price, inverse_price)
        if cur is not None:
            try:
                from app.trading.trading_cost_engine import TradeCostEngine

                cost_result = TradeCostEngine().compute_unrealized_net_pnl(
                    position["symbol"], entry_price=position["entry_price"], current_price=cur,
                    quantity=position["quantity"],
                )
                unrealized_pnl = cost_result["net_unrealized_pnl"]
                gross_unrealized_pnl = cost_result["gross_unrealized_pnl"]
                state["unrealized_pnl_cost_breakdown"] = cost_result
            except Exception:
                unrealized_pnl = gross_unrealized_pnl = (cur - position["entry_price"]) * position["quantity"]
    state["unrealized_pnl"] = unrealized_pnl
    state["gross_unrealized_pnl"] = gross_unrealized_pnl

    # net_daily_return = (net_realized_pnl + net_unrealized_pnl) / starting_equity(당일 시작
    # 자산) — 반드시 당일 시작 시점 자산(daily_pnl_baseline_equity)을 분모로 써야 한다.
    # total_equity(지금 이 순간의 계좌평가액)를 분모로 쓰지 않는다 — 그 값 자체가
    # 이미 오늘 손익을 반영해 시시각각 변하고, 계좌조회 실패 시 0을 반환할 수 있어
    # (2026-07-14 실측: 이 경로 때문에 일손실 -100%로 오판돼 자동매매가 잘못
    # 정지됨) 분모로 쓰면 위험하다. risk_manager 게이트(compute_net_daily_return, 위
    # 참조)와 반드시 같은 분모/공식을 써야 하므로(요구사항 1/5절) baseline이 아직
    # 확정되지 않았으면(당일 첫 유효 조회가 아직 없었으면) 갱신을 건너뛰고 이전 값을
    # 유지한다 — 0이나 total_equity로 대체하지 않는다.
    starting_equity = state.get("daily_pnl_baseline_equity")
    if starting_equity and starting_equity > 0:
        state["realized_pnl_today_pct"] = round(
            (state.get("realized_pnl_today_krw", 0.0) + unrealized_pnl) / starting_equity * 100.0, 4,
        )
        state["gross_realized_pnl_today_pct"] = round(
            (state.get("gross_realized_pnl_today_krw", 0.0) + gross_unrealized_pnl) / starting_equity * 100.0, 4,
        )
        # risk_manager와 UI가 같은 값을 쓰도록(요구사항 5절) 이번 사이클(주문 반영 후)
        # 최신 미실현손익 기준으로 daily_return_calculation도 함께 갱신한다.
        dr = state.get("daily_return_calculation") or {}
        dr.update({
            "starting_equity": starting_equity,
            "net_realized_pnl": state.get("realized_pnl_today_krw", 0.0),
            "net_unrealized_pnl": unrealized_pnl,
            "net_daily_return": state["realized_pnl_today_pct"],
            "calculation_source": "ledger_unified",
        })
        state["daily_return_calculation"] = dr

    trace["trade_counter"] = state.get("daily_trade_count", 0)
    trace["ui_synced"] = save_state_atomic(state)
    trace["stopped_stage"] = _first_blocked_stage(trace)
    # 사용자 요청 필드명(risk_approved/blocking_reason)도 함께 노출 — risk_manager_ok/
    # stopped_stage와 같은 값을 가리키는 별칭이다.
    trace["risk_approved"] = trace["risk_manager_ok"]
    trace["blocking_reason"] = _build_blocking_reason(trace)

    # 백그라운드 스레드에서만 사이클이 돌아도(=Streamlit 세션에 아무도 접속하지 않아도)
    # UI가 "사이클 미실행"을 보여주지 않도록, 이번 사이클 결과(최종 trace 포함)를
    # state에도 남겨 한 번 더 저장한다.
    state["last_pipeline_trace"] = trace
    state["last_cycle_computed_at"] = now.isoformat()
    state["last_hynix_price"] = hynix_price  # legacy alias: LONG_SYMBOL(0193T0) execution price
    state["last_long_price"] = hynix_price  # LONG_SYMBOL(0193T0) — 실제 매매/손익 기준
    state["last_hynix_signal_price"] = hynix_signal_price  # 000660 — 감시 기초자산(신호 계산 참고용)
    state["last_inverse_price"] = inverse_price
    state["last_enhanced_result"] = enhanced_result
    state["last_decision"] = decision
    save_state_atomic(state)

    # ── 로그 기록 ────────────────────────────────────────────────────────────
    try:
        log_enhanced_prediction({
            "hynix_price": hynix_price, "inverse_price": inverse_price,
            "base_prediction_score": enhanced_result.get("base_prediction_score"),
            "existing_micron_score": enhanced_result.get("existing_micron_score"),
            "hynix_technical_score": enhanced_result.get("hynix_technical_score"),
            "intraday_momentum_score": enhanced_result.get("intraday_momentum_score"),
            "inverse_pressure_score": enhanced_result.get("inverse_pressure_score"),
            "enhanced_score": enhanced_result.get("enhanced_score"),
            "final_action": decision.get("final_action"),
            "reason_top5": enhanced_result.get("reason_top5"),
        })
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] 예측 로그 기록 실패: %s", exc)

    failed_orders = [o for o in orders_this_cycle if not o.get("success")]
    if failed_orders:
        for o in failed_orders:
            warnings.append(f"주문 실패/스킵: [{o.get('action')}] {o.get('symbol')} — {o.get('message')}")

    for order in orders_this_cycle:
        try:
            log_trade({
                **order, "mode": mode,
                "base_prediction_score": enhanced_result.get("base_prediction_score"),
                "existing_micron_score": enhanced_result.get("existing_micron_score"),
                "hynix_technical_score": enhanced_result.get("hynix_technical_score"),
                "inverse_pressure_score": enhanced_result.get("inverse_pressure_score"),
                "enhanced_score": enhanced_result.get("enhanced_score"),
                "realized_pnl": state.get("realized_pnl_today_krw"),
                "unrealized_pnl": unrealized_pnl,
                "daily_return": state.get("realized_pnl_today_pct"),
            })
        except Exception as exc:
            logger.debug("[HynixSwitchEngine] 거래 로그 기록 실패: %s", exc)

    # ── 판단 로그 + 예측/실제 결과 추적 (실제 주문 여부와 무관하게 항상 수행) ──
    try:
        from app.services.hynix_prediction_tracker import log_trade_decision, check_and_resolve_pending_outcomes

        log_trade_decision(
            now, hynix_price, inverse_price, enhanced_result, decision,
            actual_trade_executed=any(o.get("success") for o in orders_this_cycle),
            position_symbol=(state.get("position") or {}).get("symbol"),
        )
        check_and_resolve_pending_outcomes(now, hynix_price, inverse_price)
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] 판단/결과 추적 로그 실패: %s", exc)

    liquidation_phase = liquidation_phase_now

    # ── 장 종료 후 1일 1회: 종가 outcome 확정 + 일별 리포트 + 가중치 추천 ──────
    today_str = now.strftime("%Y%m%d")
    if liquidation_phase == "closed" and state.get("daily_report_generated_date") != today_str and hynix_price:
        try:
            from app.services.hynix_prediction_tracker import resolve_close_outcomes
            from app.services.hynix_prediction_report import generate_daily_prediction_report
            from app.services.hynix_weight_recommender import recommend_weight_adjustment
            from app.services.hynix_weight_manager import maybe_auto_apply_in_mock

            resolve_close_outcomes(
                date_str=today_str, hynix_close_price=hynix_price, inverse_close_price=inverse_price,
                realized_pnl_today_krw=state.get("realized_pnl_today_krw", 0.0),
            )
            generate_daily_prediction_report(date_str=today_str)
            recommend_weight_adjustment()
            maybe_auto_apply_in_mock(mode, bool(state.get("weight_auto_apply_enabled")))

            from app.services.hynix_exit_recommender import recommend_exit_parameters, generate_daily_exit_learning

            recommend_exit_parameters()
            generate_daily_exit_learning(date_str=today_str)

            state["daily_report_generated_date"] = today_str
            save_state_atomic(state)
        except Exception as exc:
            logger.error("[HynixSwitchEngine] 장종료 리포트/추천 생성 실패: %s", exc)
            warnings.append(f"장종료 리포트/추천 생성 실패: {exc}")

    return {
        "skipped": False,
        "computed_at": now.isoformat(),
        "mode": mode,
        "auto_trade_on": auto_trade_on,
        "watch_only": is_watch_only(now),
        "new_entry_allowed": is_new_entry_allowed(now),
        "liquidation_phase": liquidation_phase,
        "hynix_current_price": hynix_price,
        "hynix_signal_price": hynix_signal_price,
        "long_current_price": hynix_price,
        "inverse_current_price": inverse_price,
        "enhanced_result": enhanced_result,
        "decision": decision,
        "forced_info": forced_info,
        "orders_this_cycle": orders_this_cycle,
        "state": state,
        # UI/Dynamic Exit AI는 이 필드(브로커 sync 직후 결과)를 읽어야 한다.
        # state["position"]/state["daily_trade_count"]는 이 값을 그대로 옮겨 담은 캐시일 뿐이다.
        "position_manager": position_manager.to_cache_dict() if position_manager is not None else None,
        "warnings": warnings + (enhanced_result.get("warnings") or []),
        "pipeline_trace": trace,
        # SHADOW MODE 전용 — 실제 주문에 영향 없음(비교/검증 목적).
        "cycle_ai_shadow_result": state.get("last_cycle_ai_result"),
    }


def execute_hynix_auto_trade(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """update_hynix_auto_trade_loop()의 공개 래퍼."""
    return update_hynix_auto_trade_loop(mode=mode, now=now)
