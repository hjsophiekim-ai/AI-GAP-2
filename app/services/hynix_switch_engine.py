"""
hynix_switch_engine.py — 하이닉스⇄인버스 Enhanced 자동매매 오케스트레이터.

3분마다(또는 UI 자동새로고침 주기마다) 아래 순서를 반복한다:
① kospilab 갱신 ② 마이크론 실시간 갱신 ③~⑥ 점수/판단 계산 ⑦ 보유종목 확인
⑧ 강제청산/TP·SL/스위칭 실행 ⑨ 로그 기록 ⑩ 결과 반환(UI 렌더링용).

각 단계는 개별 try/except로 감싸 부분 실패해도 나머지는 계속 진행한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from app.logger import logger
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL
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

_PULLBACK_MORNING_WINDOW_END = "10:00"
_PULLBACK_PATIENCE_MINUTES = 15


def evaluate_pullback_gate(state: dict, desired_symbol: str, final_action: str, now: datetime, forced_info: dict, hynix_df_1min, mode: str) -> dict:
    """신규 진입(매수) 전 눌림목 부근인지 확인한다.

    09:10~10:00 구간은 그 창이 끝날 때까지, 그 외 시간대는 신호 발생 후 최대
    `_PULLBACK_PATIENCE_MINUTES`분까지 눌림목을 기다린다. 강제거래창이 먼저
    끝나면 그 마감시각을 데드라인으로 우선한다. 데드라인 도달 시 무조건 진입(진행)한다.
    """
    pending = state.get("pending_entry")
    if not pending or pending.get("action") != final_action or pending.get("symbol") != desired_symbol:
        pending = {"action": final_action, "symbol": desired_symbol, "since": now.isoformat()}
        state["pending_entry"] = pending

    try:
        since = datetime.fromisoformat(pending["since"])
    except Exception:
        since = now

    signal_started_in_morning_window = _parse_hm("09:10") <= since.time() < _parse_hm(_PULLBACK_MORNING_WINDOW_END)
    if signal_started_in_morning_window:
        deadline = datetime.combine(since.date(), _parse_hm(_PULLBACK_MORNING_WINDOW_END))
    else:
        deadline = since + timedelta(minutes=_PULLBACK_PATIENCE_MINUTES)

    window = forced_info.get("window")
    if window:
        try:
            _, end_str = window.split("-")
            window_deadline = datetime.combine(now.date(), _parse_hm(end_str))
            deadline = min(deadline, window_deadline)
        except Exception:
            pass

    if now >= deadline:
        return {"proceed": True, "message": f"눌림목 대기 데드라인({deadline.strftime('%H:%M')}) 도달 — 강제 진입"}

    if desired_symbol == HYNIX_SYMBOL:
        df_for_check = hynix_df_1min
    else:
        df_for_check = _load_inverse_1min_for_pullback(mode)

    pullback = detect_pullback(df_for_check)
    if pullback.get("is_pullback"):
        return {"proceed": True, "message": f"눌림목 진입 조건 충족: {pullback.get('reason')}"}
    return {
        "proceed": False,
        "message": f"눌림목 대기 중({pullback.get('reason')}) — 데드라인 {deadline.strftime('%H:%M')}까지 대기",
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


def _daily_pnl_pct(state: dict, total_equity: Optional[float]) -> Optional[float]:
    if total_equity is None:
        return None
    baseline = state.get("daily_pnl_baseline_equity")
    if not baseline:
        state["daily_pnl_baseline_equity"] = total_equity
        return 0.0
    if baseline <= 0:
        return 0.0
    return (total_equity / baseline - 1.0) * 100.0


def update_hynix_auto_trade_loop(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """1회 실행 사이클. mode가 None이면 state에 저장된 mode를 사용.

    `now`는 테스트에서 시각을 주입하기 위한 선택 인자이며, 운영 시에는 항상 현재시각이 쓰인다.
    """
    warnings: list[str] = []
    now = now or datetime.now()
    state = load_state(mode=mode)
    mode = mode or state.get("mode", "mock")
    state["mode"] = mode

    if state.get("stopped"):
        return {"skipped": True, "reason": state.get("stopped_reason") or "자동매매 정지 상태", "state": state}

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

    hynix_price = enhanced_result.get("hynix_current_price")
    inverse_price = enhanced_result.get("inverse_current_price")
    df_1min = (enhanced_result.get("market_data") or {}).get("hynix_minute", {}).get("df_1min")

    price_data_ok = hynix_price is not None
    order_api_ok = True
    broker = None
    real_gate_ok = True

    auto_trade_on = bool(state.get("auto_trade_on"))
    position_manager = None
    if auto_trade_on:
        try:
            if mode == "real":
                from app.config import get_config
                from app.trading.broker_factory import create_broker

                cfg = get_config()
                real_gate_ok = cfg.full_auto_real_confirm_ok()
                if not real_gate_ok:
                    warnings.append("REAL 완전자동 게이트 미충족(safety.enable_real_trading / FULL_AUTO_REAL_CONFIRM_TEXT) — 주문 실행 생략")
                if real_gate_ok:
                    broker = create_broker(
                        cfg, mode="real",
                        runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
                    )
            else:
                # mock은 KIS 모의투자 서버(계좌 권한/외부 상태에 의존)를 거치지 않고,
                # 사용자가 설정한 예산으로 완전히 로컬에서 동작하는 DryRunBroker를 사용한다.
                # → KIS 모의계좌 승인/장시간 이슈와 무관하게 항상 자동매매가 동작한다.
                from app.trading.dry_run_broker import DryRunBroker

                broker = DryRunBroker(initial_balance=float(state.get("mock_budget_krw", 10_000_000.0)))

            if broker is not None:
                # Broker가 유일한 Source of Truth — position_manager.sync()로 실제 포지션을
                # 먼저 확정하고, state는 그 결과를 담는 캐시로만 갱신한다.
                position_manager = HynixPositionManager(broker, mode=mode)
                position_manager.sync(force=True)
                apply_position_manager_to_state(state, position_manager)
                if state.get("position_conflict"):
                    warnings.append(state.get("critical_alert") or "000660/0197X0 동시 보유 감지 — 신규매수 금지")
        except Exception as exc:
            order_api_ok = False
            warnings.append(f"브로커 초기화 실패: {exc}")
            logger.error("[HynixSwitchEngine] 브로커 초기화 실패: %s", exc)

    total_equity = None
    daily_pnl_pct = None
    if broker is not None:
        try:
            positions = broker.get_positions()
            cash = broker.get_buyable_cash()
            total_equity = float(cash) + sum(p.market_value for p in positions)
            is_mock_override = mode == "mock" and state.get("allow_mock_loss_override")
            daily_pnl_pct = _daily_pnl_pct(state, total_equity)
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

    orders_this_cycle: list = []
    attempted_entry = False
    trading_allowed = auto_trade_on and real_gate_ok and not state.get("stopped") and not is_watch_only(now) and broker is not None

    if trading_allowed:
        try:
            liq = run_liquidation_if_needed(now, state, broker, hynix_price, inverse_price)
            orders_this_cycle.extend(liq.get("orders", []))
        except Exception as exc:
            logger.error("[HynixSwitchEngine] 강제청산 처리 실패: %s", exc)
            warnings.append(f"강제청산 처리 실패: {exc}")
            liq = {"liquidated": False}

        if not liq.get("liquidated"):
            try:
                tp_sl = run_tp_sl_if_needed(state, broker, hynix_price, inverse_price)
                orders_this_cycle.extend(tp_sl.get("orders", []))
            except Exception as exc:
                logger.error("[HynixSwitchEngine] TP/SL 처리 실패: %s", exc)
                warnings.append(f"TP/SL 처리 실패: {exc}")
                tp_sl = {"triggered": False}

            if not tp_sl.get("triggered"):
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
                    if is_new_entry and state.get("position_conflict"):
                        proceed = False
                        warnings.append("포지션 동기화 필요(000660/0197X0 동시 보유) — 신규매수 금지")
                    elif is_new_entry:
                        try:
                            gate = evaluate_pullback_gate(state, desired_symbol, final_action, now, forced_info, df_1min, mode)
                            proceed = gate["proceed"]
                            if not proceed:
                                warnings.append(gate["message"])
                        except Exception as exc:
                            logger.error("[HynixSwitchEngine] 눌림목 게이트 판단 실패, 즉시 진입으로 폴백: %s", exc)
                            proceed = True

                    if proceed:
                        attempted_entry = True
                        state.pop("pending_entry", None)
                        try:
                            switch = run_switch_or_entry(
                                state, broker, final_action, hynix_price, inverse_price,
                                now=now, forced=forced, reason=reason,
                            )
                            orders_this_cycle.extend(switch.get("orders", []))
                        except Exception as exc:
                            logger.error("[HynixSwitchEngine] 스위칭/진입 처리 실패: %s", exc)
                            warnings.append(f"스위칭/진입 처리 실패: {exc}")
                else:
                    state.pop("pending_entry", None)

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

    # ── 미실현손익/당일수익률 갱신 ────────────────────────────────────────────
    position = state.get("position") or {}
    unrealized_pnl = 0.0
    if position.get("symbol") and (position.get("quantity") or 0) > 0 and position.get("entry_price"):
        cur = _current_price(position["symbol"], hynix_price, inverse_price)
        if cur is not None:
            unrealized_pnl = (cur - position["entry_price"]) * position["quantity"]
    state["unrealized_pnl"] = unrealized_pnl

    if total_equity:
        state["realized_pnl_today_pct"] = round(
            (state.get("realized_pnl_today_krw", 0.0) + unrealized_pnl) / total_equity * 100.0, 4,
        )

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

    liquidation_phase = get_liquidation_phase(now)

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
    }


def execute_hynix_auto_trade(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """update_hynix_auto_trade_loop()의 공개 래퍼."""
    return update_hynix_auto_trade_loop(mode=mode, now=now)
