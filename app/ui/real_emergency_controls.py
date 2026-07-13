from __future__ import annotations

from datetime import datetime

import streamlit as st

from app.config import get_config
from app.trading.broker_factory import create_broker
from app.trading.emergency_stop import (
    activate_emergency_stop,
    clear_emergency_stop,
    emergency_liquidation_allowed,
    get_emergency_stop_state,
)
from app.models import OrderResult


def _stop_auto_services(reason: str) -> None:
    st.session_state["real_mode_enabled"] = False
    st.session_state["enable_real_buy"] = False
    st.session_state["enable_real_sell"] = False
    st.session_state["intraday_auto_running"] = False

    try:
        from app.services.hynix_auto_trade_service import stop_auto_trade
        stop_auto_trade()
    except Exception:
        pass

    try:
        from app.services.hynix_switch_engine import set_control
        set_control(auto_trade_on=False)
    except Exception:
        pass

    try:
        from app.services.hynix_switch_state import load_state, save_state_atomic
        state = load_state()
        state["auto_trade_on"] = False
        state["stopped"] = True
        state["stopped_reason"] = reason
        save_state_atomic(state)
    except Exception:
        pass


def _result_to_row(result: OrderResult) -> dict:
    return {
        "symbol": result.symbol,
        "name": result.name,
        "side": result.side,
        "quantity": result.quantity,
        "price": result.price,
        "success": result.success,
        "order_id": result.order_id,
        "message": result.message,
    }


def _liquidate_all_real_positions() -> list[OrderResult]:
    cfg = get_config()
    broker = create_broker(
        cfg=cfg,
        mode="real",
        confirm_text=cfg.real_confirm_text(),
        runtime_real_mode=True,
        runtime_enable_real_buy=False,
        runtime_enable_real_sell=True,
    )
    positions = broker.get_positions()
    results: list[OrderResult] = []
    with emergency_liquidation_allowed():
        for pos in positions:
            price = pos.current_price or pos.avg_price or 0
            results.append(
                broker.sell(
                    symbol=pos.symbol,
                    name=pos.name,
                    quantity=pos.quantity,
                    price=price,
                    order_type="market",
                )
            )
    return results


def render_real_emergency_stop(prefix: str = "main") -> None:
    state = get_emergency_stop_state()
    if state.get("active"):
        st.error(
            f"REAL 자동매매 긴급정지 활성화: {state.get('reason', '')} "
            f"{state.get('activated_at', '')}"
        )
    else:
        st.caption("REAL 자동매매 긴급정지는 비활성 상태입니다.")

    with st.expander("REAL 자동매매 긴급정지", expanded=bool(state.get("active"))):
        st.warning(
            "클릭하면 신규 REAL 주문을 즉시 차단하고 자동매매를 정지합니다. "
            "전량청산 옵션은 보유종목 전체에 시장가 매도를 시도합니다."
        )
        choice = st.radio(
            "정지 방식",
            ["A. 자동매매만 정지", "B. 보유종목 전량청산 후 정지"],
            key=f"{prefix}_real_emergency_choice",
        )
        confirm = st.checkbox("위 동작을 이해했고 즉시 실행합니다.", key=f"{prefix}_real_emergency_confirm")

        if st.button(
            "REAL 자동매매 긴급정지",
            type="primary",
            use_container_width=True,
            disabled=not confirm,
            key=f"{prefix}_real_emergency_btn",
        ):
            reason = f"manual emergency stop at {datetime.now().isoformat(timespec='seconds')}"
            activate_emergency_stop(reason=reason)
            _stop_auto_services(reason)
            st.session_state["real_emergency_last_action"] = choice

            if choice.startswith("B."):
                try:
                    with st.spinner("보유종목 전량청산 주문 실행 중..."):
                        results = _liquidate_all_real_positions()
                    st.session_state["real_emergency_liquidation_results"] = [
                        _result_to_row(r) for r in results
                    ]
                    ok = sum(1 for r in results if r.success)
                    st.success(f"긴급정지 완료. 전량청산 주문 결과: 성공 {ok}건 / 전체 {len(results)}건")
                except Exception as exc:
                    st.error(f"긴급정지는 활성화됐지만 전량청산 실행 중 오류가 발생했습니다: {exc}")
            else:
                st.success("긴급정지 완료. 자동매매와 신규 REAL 주문을 차단했습니다.")
            st.rerun()

        rows = st.session_state.get("real_emergency_liquidation_results")
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)

        if state.get("active"):
            if st.button("긴급정지 상태 해제", key=f"{prefix}_real_emergency_clear"):
                clear_emergency_stop()
                st.success("긴급정지 상태를 해제했습니다.")
                st.rerun()
