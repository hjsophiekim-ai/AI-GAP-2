"""
9_SK하이닉스_자동매매.py — SK하이닉스 예측 기반 자동매매 (제안 + 승인 모드).

기본값은 PAPER(KIS 모의투자)이며, REAL 주문은 별도 토글 + 확인 문구 입력을
거쳐야만 실행됩니다. 완전자동(ENABLE_FULL_AUTO)은 .env 설정으로만 활성화되며,
이 페이지에서는 제안 생성과 승인 후 실행(PAPER/REAL)만 제공합니다.

실전 주문은 사용자 책임이며, 이 페이지의 모든 결과는 확률 기반 참고자료입니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import os
from datetime import datetime
from datetime import time as dtime_cls

import streamlit as st

from app.config import get_config
from app.services.hynix_auto_trade_service import (
    generate_trade_proposal,
    execute_proposal,
    stop_auto_trade,
    resume_auto_trade,
    is_stopped,
)

st.title("SK하이닉스 예측 기반 자동매매")
st.caption("주문 제안 + 사용자 승인 모드. PAPER가 기본값이며, REAL 주문은 별도 확인이 필요합니다.")
st.info(
    "⚠️ 모든 결과는 확률 기반 참고자료이며 투자판단은 사용자 책임입니다. "
    "\"확정 수익\"/\"무조건 상승\"을 의미하지 않습니다.",
    icon="⚠️",
)

cfg = get_config()


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _fmt_num(value, fmt: str = ".1f", suffix: str = "", empty: str = "—") -> str:
    if value is None:
        return empty
    try:
        return f"{float(value):{fmt}}{suffix}"
    except Exception:
        return empty


def _safe_real_gate_status(cfg_obj, current_mode: str = "real") -> dict:
    """Return REAL auto-trade gate diagnostics without assuming a new Config API.

    Older Streamlit worker processes can keep an old app.config module loaded.
    In that case cfg.enhanced_real_gate_status may be missing even though the
    current source has it. Keep the page alive and show a conservative
    diagnostic instead of raising AttributeError.
    """
    if hasattr(cfg_obj, "enhanced_real_gate_status"):
        try:
            return cfg_obj.enhanced_real_gate_status(current_mode=current_mode)
        except Exception as exc:
            return {
                "ready": False,
                "checks": {"enhanced_real_gate_status_callable": False},
                "blocking_reasons": [f"REAL_GATE_DIAGNOSTIC_ERROR: {exc}"],
                "diagnostic_error": str(exc),
            }

    import os

    def _env_bool(name: str) -> bool:
        return os.getenv(name, "").strip().lower() in ("true", "1", "yes")

    def _present(name: str) -> bool:
        return bool(os.getenv(name, "").strip())

    config_real_enabled = (
        bool(cfg_obj.real_trading_enabled())
        if hasattr(cfg_obj, "real_trading_enabled")
        else bool(getattr(cfg_obj, "safety", {}).get("enable_real_trading", False))
    )
    real_start_date = (
        cfg_obj.real_trading_start_date()
        if hasattr(cfg_obj, "real_trading_start_date")
        else str(getattr(cfg_obj, "safety", {}).get("real_trading_start_date", "2026-07-14"))
    )
    real_date_allowed = (
        bool(cfg_obj.real_trading_date_allowed())
        if hasattr(cfg_obj, "real_trading_date_allowed")
        else True
    )
    checks = {
        "current_mode_is_real": current_mode == "real",
        "config_or_env_real_trading_enabled": config_real_enabled,
        "real_trading_start_date_allowed": real_date_allowed,
        "enable_full_auto": _env_bool("ENABLE_FULL_AUTO"),
        "env_enable_real_trading": _env_bool("ENABLE_REAL_TRADING"),
        "enable_real_buy": _env_bool("ENABLE_REAL_BUY"),
        "enable_real_sell": _env_bool("ENABLE_REAL_SELL"),
        "real_app_key_present": _present("KIS_REAL_APP_KEY"),
        "real_app_secret_present": _present("KIS_REAL_APP_SECRET"),
        "real_account_present": any(_present(name) for name in ("KIS_REAL_ACCOUNT_NO", "KIS_REAL_CANO", "KIS_ACCOUNT_NO")),
        "real_product_code_present": any(_present(name) for name in ("KIS_REAL_ACCOUNT_PRODUCT_CODE", "KIS_REAL_ACNT_PRDT_CD", "KIS_ACCOUNT_PRODUCT_CODE")),
    }
    blocking_map = {
        "current_mode_is_real": "CURRENT_MODE_NOT_REAL",
        "config_or_env_real_trading_enabled": "REAL_TRADING_DISABLED",
        "real_trading_start_date_allowed": f"REAL_TRADING_START_DATE_NOT_REACHED({real_start_date})",
        "enable_full_auto": "ENABLE_FULL_AUTO_NOT_TRUE",
        "env_enable_real_trading": "ENV_ENABLE_REAL_TRADING_NOT_TRUE",
        "enable_real_buy": "ENABLE_REAL_BUY_NOT_TRUE",
        "enable_real_sell": "ENABLE_REAL_SELL_NOT_TRUE",
        "real_app_key_present": "KIS_REAL_APP_KEY_MISSING",
        "real_app_secret_present": "KIS_REAL_APP_SECRET_MISSING",
        "real_account_present": "KIS_REAL_ACCOUNT_MISSING",
        "real_product_code_present": "KIS_REAL_PRODUCT_CODE_MISSING",
    }
    blocking_reasons = [reason for key, reason in blocking_map.items() if not checks.get(key)]
    return {
        "ready": not blocking_reasons,
        "checks": checks,
        "blocking_reasons": blocking_reasons,
        "fallback_diagnostic": "Config.enhanced_real_gate_status missing; restart Streamlit to load latest app.config.",
        "final_safety_enable_real_trading": config_real_enabled,
        "real_trading_start_date": real_start_date,
        "real_trading_date_allowed": real_date_allowed,
    }


def _krx_order_window_status(now: datetime | None = None) -> dict:
    """Approximate SK Hynix real-order timing used by the enhanced engine."""
    now = now or datetime.now()
    t = now.time()
    is_weekday = now.weekday() < 5
    market_open = is_weekday and dtime_cls(9, 0) <= t <= dtime_cls(15, 30)
    new_entry_allowed = is_weekday and dtime_cls(9, 10) <= t < dtime_cls(14, 50)
    liquidation_only = is_weekday and dtime_cls(14, 50) <= t < dtime_cls(15, 20)
    return {
        "market_open": market_open,
        "new_entry_allowed": new_entry_allowed,
        "liquidation_only": liquidation_only,
        "can_send_real_order_now": new_entry_allowed or liquidation_only,
        "message": (
            "KRX new entries allowed now (09:10-14:50)."
            if new_entry_allowed else
            "KRX liquidation/position-management window only (14:50-15:20)."
            if liquidation_only else
            "Outside KRX real-order window. Real account gate can be ready, but orders should wait for the next session."
        ),
    }

if is_stopped():
    st.error("🛑 자동매매가 정지 상태입니다. 아래 '자동매매 재개' 버튼을 눌러야 새 제안을 생성합니다.")

# ─────────────────────────────────────────────────────────────────────────────
# 상단 컨트롤
# ─────────────────────────────────────────────────────────────────────────────

c1, c2, c3 = st.columns([2, 1, 1])
with c1:
    gen_clicked = st.button("제안 생성 (PAPER 계좌 기준)", type="primary", use_container_width=True)
with c2:
    if st.button("자동매매 정지", use_container_width=True):
        stop_auto_trade()
        st.rerun()
with c3:
    if st.button("자동매매 재개", use_container_width=True):
        resume_auto_trade()
        st.rerun()

st.caption(f"완전자동(ENABLE_FULL_AUTO) 상태: {'활성화' if cfg.full_auto_enabled() else '비활성화(기본값)'}")

if gen_clicked:
    with st.spinner("데이터 수집 및 제안 생성 중..."):
        st.session_state["hynix_auto_proposal"] = generate_trade_proposal(mode="mock")

proposal = st.session_state.get("hynix_auto_proposal")

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# 제안 결과 표시
# ─────────────────────────────────────────────────────────────────────────────

if not proposal:
    st.info("'제안 생성' 버튼을 눌러 매매 제안을 확인하세요.")
else:
    if proposal.get("blocked"):
        missing = proposal.get("missing_data") or []
        st.error(
            f"🔴 **제안을 생성할 수 없습니다.** {proposal.get('block_reason', '')}"
            + (f"\n\n누락된 데이터: {', '.join(missing)}" if missing else "")
        )
    else:
        # a~e: 현재가/고점저점/하락률/수익률/score
        m1, m2, m3, m4, m5 = st.columns(5)
        with m1:
            st.metric("현재가", f"{proposal['current_price']:,.0f}원" if proposal.get("current_price") else "—")
        with m2:
            st.metric("최근 고점/저점", f"{proposal.get('recent_high') or 0:,.0f} / {proposal.get('recent_low') or 0:,.0f}")
        with m3:
            dd = proposal.get("drawdown_rate")
            st.metric("고점 대비 하락률", f"{dd:.1f}%" if dd is not None else "—")
        with m4:
            pr = proposal.get("profit_rate")
            st.metric("평균매수가 대비 수익률", f"{pr:.1f}%" if pr is not None else "보유 없음")
        with m5:
            st.metric("단기 방향 점수", f"{proposal.get('short_term_score', 0):.0f}/100")

        # f, g: 판단 + 제안 금액/비율
        action = proposal.get("action", "HOLD")
        action_label = {"BUY": "🟢 매수 제안", "SELL": "🔴 매도 제안", "HOLD": "⚪ 대기"}.get(action, action)
        st.subheader(f"매매 판단: {action_label}")
        if action == "BUY":
            st.markdown(f"**제안 매수금액:** {proposal.get('buy_cash_amount', 0):,.0f}원")
        elif action == "SELL":
            st.markdown(f"**제안 매도비중:** {proposal.get('sell_quantity_ratio', 0)*100:.0f}%")
        st.caption(f"세부 판단: {proposal.get('judgement', '—')}")

        # h, i, j: 지지선/목표가/확률
        st.markdown("**예상 지지선 3개**")
        sup_cols = st.columns(3)
        for col, label, val in zip(sup_cols, ["지지선 1", "지지선 2", "지지선 3"], proposal.get("support_levels") or [None, None, None]):
            with col:
                st.metric(label, f"{val:,.0f}원" if val else "—")

        st.markdown("**예상 목표가 3개 및 도달확률**")
        tgt_cols = st.columns(3)
        targets = proposal.get("target_levels") or [None, None, None]
        probs = proposal.get("target_probabilities") or {}
        for col, label, val, key in zip(tgt_cols, ["목표가 1", "목표가 2", "목표가 3"], targets, ["target_1", "target_2", "target_3"]):
            with col:
                st.metric(label, f"{val:,.0f}원" if val else "—", delta=f"도달확률 {probs.get(key, 0):.0f}%")

        # k: 판단 근거 Top5
        with st.expander("판단 근거 Top 5", expanded=True):
            for i, reason in enumerate(proposal.get("reasons_top5") or [], start=1):
                st.markdown(f"{i}. {reason}")
            if proposal.get("sizing_reasons"):
                st.markdown("**포지션 사이징 근거:**")
                for reason in proposal["sizing_reasons"]:
                    st.markdown(f"- {reason}")

        # l: 위험 경고
        warnings = (proposal.get("risk_warnings") or []) + (proposal.get("sizing_warnings") or [])
        if proposal.get("news_warning"):
            warnings.append(proposal["news_warning"])
        if warnings:
            st.warning("**위험 경고**\n\n" + "\n".join(f"- {w}" for w in warnings))

        st.markdown(
            f"현금비중 {proposal.get('cash_ratio', 0):.1f}% · 종목비중 {proposal.get('symbol_ratio', 0):.1f}% "
            f"· 총자산 {proposal.get('total_equity', 0):,.0f}원 · 보유수량 {proposal.get('position_quantity', 0)}주"
        )
        st.caption(f"ℹ️ {proposal.get('disclaimer', '')}")

        st.divider()
        st.subheader("주문 실행")

        if action == "HOLD":
            st.info("현재 제안은 대기(HOLD)이므로 실행할 주문이 없습니다.")
        else:
            order_amount = proposal.get("buy_cash_amount", 0) if action == "BUY" else (
                (proposal.get("current_price") or 0) * (proposal.get("position_quantity", 0) * proposal.get("sell_quantity_ratio", 0))
            )
            est_qty = int(order_amount // proposal["current_price"]) if action == "BUY" and proposal.get("current_price") else None
            st.markdown(
                f"**예상 주문가격:** {proposal.get('current_price', 0):,.0f}원  \n"
                + (f"**예상 수량:** {est_qty}주  \n" if est_qty is not None else "")
                + f"**예상 주문금액:** {order_amount:,.0f}원"
            )

            oc1, oc2 = st.columns(2)
            with oc1:
                if st.button("PAPER 주문 실행", type="primary", use_container_width=True, disabled=is_stopped()):
                    with st.spinner("PAPER 주문 실행 중..."):
                        result = execute_proposal(proposal, mode="mock")
                    if result.get("success"):
                        st.success(f"PAPER 주문 성공: {result}")
                    else:
                        st.error(f"PAPER 주문 실패: {result.get('message')}")

            with oc2:
                st.markdown("**REAL 주문 실행**")
                real_toggle = st.checkbox("REAL(실전) 주문 활성화", key="hynix_auto_real_toggle")
                expected_confirm = cfg.real_confirm_text()
                real_confirm_input = st.text_input(
                    f"확인 문구 입력 (정확히 `{expected_confirm}`)",
                    type="password",
                    key="hynix_auto_real_confirm_input",
                    disabled=not real_toggle,
                )
                real_ready = real_toggle and real_confirm_input == expected_confirm and cfg.real_trading_enabled()
                if real_toggle and not cfg.real_trading_enabled():
                    st.caption("config.yaml의 safety.enable_real_trading이 false입니다 — REAL 실행 불가.")
                if st.button("REAL 주문 실행", use_container_width=True, disabled=not real_ready or is_stopped()):
                    with st.spinner("REAL 주문 실행 중..."):
                        result = execute_proposal(
                            proposal, mode="real", confirm_text=real_confirm_input,
                            runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
                        )
                    if result.get("success"):
                        st.success(f"REAL 주문 성공: {result}")
                    else:
                        st.error(f"REAL 주문 실패: {result.get('message')}")

# ─────────────────────────────────────────────────────────────────────────────
# Enhanced 자동매매 (하이닉스 ⇄ SOL 인버스2X 스위칭, 당일청산)
# ─────────────────────────────────────────────────────────────────────────────

st.divider()
st.header("🔄 Enhanced 자동매매 (하이닉스 ⇄ SOL 인버스2X)")
st.caption(
    "기술점수·인버스압력점수·장중모멘텀점수를 결합한 개선된 최종점수로 하이닉스/인버스를 자동 스위칭합니다. "
    "당일 진입·당일 청산 원칙이며, 15:15 도달 시 보유 포지션은 수익/손실과 무관하게 전량 강제청산됩니다."
)

from app.services.hynix_switch_state import load_state, save_state_atomic
from app.services.hynix_switch_engine import update_hynix_auto_trade_loop, set_control, reset_mock_account
from app.trading.hynix_switch_risk_gate import is_new_entry_allowed
from app.services.hynix_auto_trade_scheduler import (
    ensure_cycle_thread_running,
    ensure_fast_trend_watcher_running,
    get_status as get_cycle_status,
)
import app.trading.hynix_big_trend_engine as bte

try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=180_000, key="hynix_switch_autorefresh")
except Exception:
    pass

switch_state = load_state()

try:
    import os as _os
    import sys as _sys
    import subprocess as _sp

    _runtime_port = _os.environ.get("STREAMLIT_SERVER_PORT")
    for _i, _arg in enumerate(_sys.argv):
        if _arg == "--server.port" and _i + 1 < len(_sys.argv):
            _runtime_port = _sys.argv[_i + 1]
        elif _arg.startswith("--server.port="):
            _runtime_port = _arg.split("=", 1)[1]
    _runtime_sha = _sp.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=_PROJECT_ROOT, timeout=5).decode().strip()
except Exception:
    _runtime_port = _runtime_port if "_runtime_port" in locals() else "unknown"
    _runtime_sha = "unknown"

_fw_state = switch_state.get("fast_trend_watcher") or {}
_fw_signal = _fw_state.get("last_signal") or {}
_rt_cols = st.columns(5)
_rt_cols[0].metric("Run port", _runtime_port or "unknown")
_rt_cols[1].metric("Git SHA", _runtime_sha)
_rt_cols[2].metric("Code path", str(_PROJECT_ROOT))
_rt_cols[3].metric("Order driver", switch_state.get("actual_order_driver") or "ENHANCED_REGIME_SWITCH")
_rt_cols[4].metric("Fast watcher", _fw_signal.get("direction") or "-", delta=f"{_fw_state.get('confirmation_count', 0)}x")
if _fw_state.get("blocked_reason"):
    st.caption(f"Fast watcher block: {_fw_state.get('blocked_reason')}")

sc1, sc2, sc3 = st.columns([1, 1, 2])
with sc1:
    auto_on = st.checkbox("Enhanced 자동매매 ON", value=switch_state.get("auto_trade_on", False), key="hynix_switch_auto_on")
with sc2:
    switch_mode = st.radio(
        "모드", ["mock", "real"],
        index=(0 if switch_state.get("mode", "mock") == "mock" else 1),
        key="hynix_switch_mode", horizontal=True,
    )
with sc3:
    if switch_mode == "real":
        real_gate_status = _safe_real_gate_status(cfg, current_mode="real")
        order_window_status = _krx_order_window_status()
        real_gate_ok = bool(real_gate_status.get("ready"))
        st.caption(
            f"REAL 완전자동 게이트: {'✅ 충족' if real_gate_ok else '❌ 미충족'} "
            "(Enhanced REAL gate 단일 진단 함수 기준)"
        )
        with st.expander("REAL 게이트 진단", expanded=not real_gate_ok):
            st.caption("Render/OS 환경변수가 존재하면 로컬 .env보다 우선합니다.")
            st.markdown(f"- loaded_config_path: `{real_gate_status.get('loaded_config_path')}`")
            st.markdown(f"- loaded_config_modified_time: `{real_gate_status.get('loaded_config_modified_time')}`")
            st.markdown(f"- final safety.enable_real_trading: `{real_gate_status.get('final_safety_enable_real_trading')}`")
            st.markdown(f"- real_trading_start_date: `{real_gate_status.get('real_trading_start_date') or 'unknown'}`")
            st.markdown(f"- real_trading_date_allowed: `{real_gate_status.get('real_trading_date_allowed')}`")
            st.markdown(f"- account_source: `{real_gate_status.get('account_source') or '—'}`")
            st.markdown(f"- masked_account: `{real_gate_status.get('masked_account') or '—'}`")
            st.markdown(f"- krx_market_open_now: `{'true' if order_window_status['market_open'] else 'false'}`")
            st.markdown(f"- krx_new_entry_allowed_now: `{'true' if order_window_status['new_entry_allowed'] else 'false'}`")
            st.markdown(f"- krx_can_send_real_order_now: `{'true' if order_window_status['can_send_real_order_now'] else 'false'}`")
            st.caption(order_window_status["message"])
            if real_gate_status.get("fallback_diagnostic"):
                st.warning(real_gate_status["fallback_diagnostic"])
            checks = real_gate_status.get("checks") or {}
            for key, value in checks.items():
                st.markdown(f"- {key}: `{'true' if value else 'false'}`")
            if real_gate_status.get("blocking_reasons"):
                st.error("blocking_reason: " + ", ".join(real_gate_status["blocking_reasons"]))

if auto_on != switch_state.get("auto_trade_on") or switch_mode != switch_state.get("mode"):
    switch_state = set_control(auto_trade_on=auto_on, mode=switch_mode)

# ── 현재 실행 모드 배너 — 항상 눈에 띄게 표시(REAL이면 빨간 경고) ────────────
_active_switch_mode = switch_state.get("mode", "mock")
if _active_switch_mode == "real":
    _real_gate_status_banner = _safe_real_gate_status(cfg, current_mode="real")
    _order_window_banner = _krx_order_window_status()
    _real_gate_ok_banner = bool(_real_gate_status_banner.get("ready"))
    st.error(
        f"🔴🔴🔴 **REAL 모드 — 실제 계좌로 주문이 나갈 수 있습니다.** "
        f"REAL 완전자동 게이트: {'✅ 충족(주문 가능)' if _real_gate_ok_banner else '❌ 미충족 — 주문 최종 차단됨'}",
        icon="🚨",
    )
    if not _order_window_banner["can_send_real_order_now"]:
        st.warning("KRX order window: " + _order_window_banner["message"])
    if not _real_gate_ok_banner:
        st.warning("REAL gate blocking_reason: " + ", ".join(_real_gate_status_banner.get("blocking_reasons") or ["UNKNOWN"]))
else:
    st.warning("🟡 MOCK 모드 — KIS 모의투자 계좌로 주문이 전송됩니다. 실계좌 주문은 아니지만 모의투자 서버 주문입니다.")

if switch_state.get("mode") == "mock":
    bc1, bc2 = st.columns([2, 1])
    with bc1:
        budget_input = st.number_input(
            "Mock 자동매매 예산(원) — 이 예산 안에서만 자동매매가 실행됩니다",
            min_value=100_000, step=100_000,
            value=int(switch_state.get("mock_budget_krw", 10_000_000)), key="hynix_switch_mock_budget",
        )
        if int(budget_input) != int(switch_state.get("mock_budget_krw", 10_000_000)):
            switch_state = set_control(mock_budget_krw=budget_input)
    with bc2:
        st.markdown("&nbsp;")
        if st.button("Mock 로컬 상태 초기화", key="hynix_switch_mock_reset", use_container_width=True):
            switch_state = reset_mock_account(budget_krw=budget_input)
            st.success("Mock 로컬 상태를 초기화했습니다. KIS 모의투자 계좌 잔고는 변경하지 않습니다.")
    st.caption(
        "Mock 모드는 KIS_MOCK_ACCOUNT_NO 기준 모의투자 계좌를 사용합니다. 위 예산값은 로컬 상태/표시용이며 "
        "실제 주문가능금액은 KIS 모의투자 계좌 조회 결과를 따릅니다."
    )

    # ── Active Strategy(거래모드 기반 조기진입/Scale-in) — mock 전용 opt-in ──────
    from app.trading.hynix_trading_mode import ALL_MODES, DEFAULT_MODE

    # ── 추천 운영모드 배너(2026-07-13 사용자 요청) — mock은 기본 ACTIVE, real은 초기
    # BALANCED를 권장하고, AGGRESSIVE는 최소 5거래일 + 100건 이상 검증 전까지 비활성화를
    # 권고한다(강제 차단은 아니고 권고 — 실제 선택은 사용자 몫이나 경고는 항상 표시).
    try:
        from app.services.hynix_execution_ledger import load_ledger as _reco_load_ledger

        _reco_all = _reco_load_ledger(None)
        if not _reco_all.empty:
            _reco_live = _reco_all[(_reco_all["success"] == True) & (_reco_all["is_test_order"] != True)]  # noqa: E712
            _reco_trading_days = int(_reco_live["timestamp"].dt.strftime("%Y%m%d").nunique())
            _reco_total_trades = int(len(_reco_live))
        else:
            _reco_trading_days, _reco_total_trades = 0, 0
    except Exception:
        _reco_trading_days, _reco_total_trades = 0, 0

    _reco_mode = "ACTIVE" if switch_state.get("mode", "mock") == "mock" else "BALANCED"
    _reco_aggressive_ready = _reco_trading_days >= 5 and _reco_total_trades >= 100
    st.info(
        f"💡 **추천 운영모드**: mock 기본 `ACTIVE` / real 초기 `BALANCED` → 현재 모드({switch_state.get('mode', 'mock')}) 기준 추천값: **`{_reco_mode}`**  \n"
        f"`AGGRESSIVE`는 검증 전 비활성화 권고 — 현재 누적: {_reco_trading_days}거래일 / {_reco_total_trades}건 "
        f"(요건: 5거래일 이상 & 100건 이상) → {'✅ 검증 요건 충족' if _reco_aggressive_ready else '❌ 아직 검증 전 — AGGRESSIVE 비권장'}",
        icon="💡",
    )
    if switch_state.get("trading_mode") == "AGGRESSIVE" and not _reco_aggressive_ready:
        st.warning("⚠️ 현재 `AGGRESSIVE` 모드가 선택되어 있으나 검증 요건(5거래일/100건)을 충족하지 못했습니다 — `BALANCED` 또는 `ACTIVE`로 되돌리는 것을 권고합니다.")

    st.markdown("##### ⚡ Active Strategy (mock 전용, 거래 빈도·기대수익률 개선)")
    as1, as2 = st.columns([1, 2])
    with as1:
        trading_mode = st.selectbox(
            "거래 모드", ALL_MODES, index=ALL_MODES.index(switch_state.get("trading_mode", DEFAULT_MODE)),
            key="hynix_trading_mode_select",
        )
        if trading_mode != switch_state.get("trading_mode"):
            switch_state["trading_mode"] = trading_mode
            save_state_atomic(switch_state)
    # ── 체크박스 세션 동기화 버그 수정(2026-07-14) ───────────────────────────
    # Streamlit은 key=가 있는 위젯의 값을 최초 렌더 이후로는 value= 파라미터를
    # 무시하고 st.session_state[key]만 신뢰한다. 이 페이지 밖(스크립트/다른 세션)에서
    # switch_state["active_strategy_enabled"] 등을 바꿔도, 이미 이 체크박스를 본 적
    # 있는 브라우저 탭은 예전 값을 계속 화면에 남겨두다가 다음 rerun에서 그 예전
    # 값을 다시 파일에 덮어써버린다(실측: 백엔드에서 끈 설정이 몇 분 뒤 다시 켜짐).
    # _toggle_gen을 위젯 key에 포함시켜, 외부에서 값이 바뀔 때마다 _toggle_gen을
    # 올리면 Streamlit이 이를 "새 위젯"으로 취급해 value=(=파일의 최신값)로 다시
    # 초기화한다 — 그 뒤에는 다시 사용자의 클릭이 정상적으로 우선한다.
    _toggle_gen = switch_state.get("_toggle_gen", 0)

    with as2:
        active_enabled = st.checkbox(
            "Active Strategy로 신규진입 판단 대체(ENHANCED_LEGACY 대신 Cycle AI + Prediction V2 기반 조기진입/Scale-in 사용)",
            value=bool(switch_state.get("active_strategy_enabled", False)),
            key=f"hynix_active_strategy_toggle_{_toggle_gen}",
        )
        if active_enabled != switch_state.get("active_strategy_enabled", False):
            switch_state["active_strategy_enabled"] = active_enabled
            save_state_atomic(switch_state)
        st.caption(
            "OFF면 기존과 완전히 동일하게 동작합니다(ENHANCED_LEGACY). ON이어도 강제청산(15:15)과 "
            "레거시 TP/SL 안전망은 항상 그대로 우선 적용됩니다. real 모드에서는 이 토글이 적용되지 않습니다."
        )

    if switch_state.get("active_strategy_enabled"):
        adaptive_fusion_enabled = st.checkbox(
            "🧠 Adaptive Fusion — Prediction AI V2/Cycle AI/Micron Proxy를 실제 신규진입 판단에 융합",
            value=bool(switch_state.get("adaptive_fusion_enabled", False)),
            key=f"hynix_adaptive_fusion_toggle_{_toggle_gen}",
        )
        if adaptive_fusion_enabled != switch_state.get("adaptive_fusion_enabled", False):
            switch_state["adaptive_fusion_enabled"] = adaptive_fusion_enabled
            save_state_atomic(switch_state)
        st.caption(
            "OFF면 ACTIVE_FUSION 단독 판단(기존과 동일)이 신규진입을 결정합니다. ON이면 5개 모델을 "
            "성과기반 가중치로 융합하되, ACTIVE_FUSION을 완전히 대체하지 않습니다 — Prediction V2가 "
            "아직 SHADOW(미검증)이면 실제로는 ACTIVE_ONLY로 판단이 이루어지고 signal_source도 정직하게 "
            "ACTIVE_ONLY로 기록됩니다(적용됐다고 과장 표시하지 않음). real 모드에는 적용되지 않습니다."
        )

    big_trend_enabled = st.checkbox(
        "📈 Big Trend Holding AI — 큰 추세는 오래 보유, 작은 반대신호로 청산하지 않음(Dynamic Exit 대체)",
        value=bool(switch_state.get("big_trend_holding_enabled", False)), key="hynix_big_trend_toggle",
    )
    if big_trend_enabled != switch_state.get("big_trend_holding_enabled", False):
        switch_state["big_trend_holding_enabled"] = big_trend_enabled
        save_state_atomic(switch_state)
    st.caption(
        "OFF면 기존 Dynamic Exit AI(고정 tp_pct/sl_pct 프로필)가 청산을 그대로 담당합니다(항상 Shadow로 "
        "계산·기록은 됨). ON이면 Trend Regime별 부분익절/Profit Lock/Adaptive Trailing/추세반전확인이 "
        "청산을 대신 결정하되, 초기 손절 안전장치(effective_sl_pct)는 토글과 무관하게 항상 최우선 적용됩니다."
    )

    # ── 체크박스별 실제 영향도 표(2026-07-13 사용자 요청) — "켜져 있다"와 "실제
    # 주문/청산에 반영된다"는 다르다. 세 토글 모두 현재 구현상 mock 전용이고, real
    # 모드에서는 ON으로 저장돼 있어도 실제로는 항상 Shadow(레거시 경로 그대로 사용)다.
    _fx_active_on = bool(switch_state.get("active_strategy_enabled", False))
    _fx_fusion_on = bool(switch_state.get("adaptive_fusion_enabled", False))
    _fx_bigtrend_on = bool(switch_state.get("big_trend_holding_enabled", False))
    _fx_mode = switch_state.get("mode", "mock")
    _fx_rows = [
        ("Active Strategy", _fx_active_on, "실제 반영(mock)" if (_fx_active_on and _fx_mode == "mock") else "Shadow(적용 안 됨)", "mock 전용 — real은 항상 ENHANCED_LEGACY"),
        ("Adaptive Fusion", _fx_fusion_on, "실제 반영(mock, Active Strategy ON 시)" if (_fx_fusion_on and _fx_active_on and _fx_mode == "mock") else "Shadow(적용 안 됨)", "mock 전용 — Active Strategy가 꺼져 있으면 이 토글도 무효"),
        ("Big Trend Holding AI", _fx_bigtrend_on, "실제 반영(mock, 청산 지배)" if (_fx_bigtrend_on and _fx_mode == "mock") else "Shadow(계산·기록만)", "mock 전용 — real은 항상 Dynamic Exit AI/레거시 TP-SL"),
    ]
    st.markdown("**체크박스별 실제 영향도**")
    _fx_table = "| 기능 | 현재 ON/OFF | 실제 주문 반영 여부 | 적용 대상 |\n|---|---|---|---|\n"
    for _name, _on, _impact, _scope in _fx_rows:
        _fx_table += f"| {_name} | {'ON' if _on else 'OFF'} | {_impact} | {_scope} |\n"
    st.markdown(_fx_table)

    # ── Adaptive Fusion 진단(요구사항 6절) — 모델별 방향/확률/가중치/데이터신선도,
    # 최종합성확률, 문턱(원래/조정), 진입비중, HOLD·진입 사유, 오늘거래수/연속손실/
    # 남은거래한도, 모델불일치지수를 매 사이클 표시한다.
    _last_trend_plan = switch_state.get("last_trend_switch_plan") or {}
    _trend_freq = switch_state.get("trend_switch_frequency_state") or {}
    if _last_trend_plan:
        with st.expander("Enhanced Trend Switch 가속 진단", expanded=True):
            _ts1, _ts2, _ts3, _ts4 = st.columns(4)
            _ts1.metric("현재 우세 방향", _last_trend_plan.get("dominant_direction") or "HOLD")
            _ts2.metric(
                "연속 확인",
                f"{_last_trend_plan.get('same_direction_streak', 0)}회",
                delta=f"전환 {_last_trend_plan.get('reversal_streak', 0)}회",
            )
            _ts3.metric("즉시 전환", "YES" if _last_trend_plan.get("immediate_switch") else "NO")
            _remain = _last_trend_plan.get("pullback_wait_remaining_seconds")
            _ts4.metric("눌림목 대기", "-" if _remain is None else f"{int(_remain)}초")

            _ts5, _ts6, _ts7 = st.columns(3)
            _pct = _last_trend_plan.get("position_pct")
            _ts5.metric("진입 비중", "-" if _pct is None else f"{float(_pct) * 100:.0f}%", delta=_last_trend_plan.get("entry_type"))
            _ts6.metric("오늘 왕복거래", f"{_trend_freq.get('round_trips_today', 0)} / 8회")
            _ts7.metric("주문 차단 사유", _last_trend_plan.get("block_reason") or "없음")

    _last_fusion = switch_state.get("last_fusion_decision")
    if _fx_fusion_on and _last_fusion:
        with st.expander("🧠 Adaptive Fusion 진단(요구사항 6절)", expanded=True):
            _fc1, _fc2, _fc3, _fc4 = st.columns(4)
            _fc1.metric("최종 합성 확률", f"H{_num(_last_fusion.get('fused_hynix_probability')):.1f}/I{_num(_last_fusion.get('fused_inverse_probability')):.1f}")
            _fc2.metric(
                "문턱(조정 후/원래)",
                f"{_num(_last_fusion.get('entry_threshold_used')):.1f}% / {_num(_last_fusion.get('entry_threshold_original')):.1f}%",
                delta=f"완화 {_num(_last_fusion.get('threshold_relief_applied')):.1f}%p" if _last_fusion.get("threshold_relief_applied") else None,
            )
            _fc3.metric("진입 비중", f"{_num(_last_fusion.get('target_position_pct')):.0f}%", delta=_last_fusion.get("entry_type"))
            _fc4.metric("모델 불일치 지수", f"{_num(_last_fusion.get('disagreement_index')):.1f}")

            _fc5, _fc6, _fc7 = st.columns(3)
            _fc5.metric("오늘 거래수", f"{_last_fusion.get('orders_today_count', 0)}건 (목표 {_last_fusion.get('daily_target_trades', [4, 5])[0]}~{_last_fusion.get('daily_target_trades', [4, 5])[1]}회)")
            _fc6.metric("오늘 왕복거래", f"{_last_fusion.get('round_trips_today', 0)} / {_last_fusion.get('max_daily_round_trips', 6)}회")
            _fc7.metric("HOLD/진입 사유", "진입" if _last_fusion.get("executable") else "HOLD", delta=_last_fusion.get("blocking_reason"))

            if _last_fusion.get("strong_signal_conflict"):
                st.error("🔴 하이닉스/인버스 강신호(≥70%) 동시 충돌 — HOLD 처리됨")
            if _last_fusion.get("disagreement_override_used"):
                detail = _last_fusion.get("disagreement_override_detail") or {}
                st.info(
                    f"🧩 모델 불일치 예외 진입 사용: {detail.get('leader_model')}"
                    f"(확신도 {_num(detail.get('leader_confidence')):.0f}%) 방향, "
                    f"동조 {len(detail.get('allies', []))}개, 강반대모델 {detail.get('opposing_strong_count', 0)}개"
                )

            st.markdown("**모델별 방향·확률·가중치·데이터 신선도**")
            _diag_rows = _last_fusion.get("model_diagnostics") or []
            if _diag_rows:
                import pandas as _pd
                _diag_df = _pd.DataFrame([
                    {
                        "모델": d["model"], "방향": d.get("action", "-"),
                        "H%": d.get("hynix_probability"), "I%": d.get("inverse_probability"), "Hold%": d.get("hold_probability"),
                        "확신도": d.get("confidence"), "가중치%": d.get("weight_pct"),
                        "데이터신선도": d.get("data_quality"), "상태": d.get("model_status", "미가용" if not d.get("available") else ""),
                    }
                    for d in _diag_rows
                ])
                st.dataframe(_diag_df, use_container_width=True, hide_index=True)

            _live_trend = _last_fusion.get("live_hynix_trend") or switch_state.get("last_live_hynix_trend") or {}
            if _live_trend:
                st.markdown("**Live Hynix Trend / Data Age**")
                _lt1, _lt2, _lt3, _lt4 = st.columns(4)
                _lt1.metric("Direction", _live_trend.get("direction", "-"), delta=f"age={_live_trend.get('age_minutes')}")
                _lt2.metric("1m/3m", f"{(_live_trend.get('returns') or {}).get('1m')} / {(_live_trend.get('returns') or {}).get('3m')}")
                _lt3.metric("5m/15m", f"{(_live_trend.get('returns') or {}).get('5m')} / {(_live_trend.get('returns') or {}).get('15m')}")
                _lt4.metric("VWAP/EMA", "UP" if _live_trend.get("above_vwap") else "DOWN", delta=_live_trend.get("ema_slope_pct"))
                st.caption("Top factors: " + ", ".join(_last_fusion.get("top_decision_factors") or _live_trend.get("top_factors") or []))

            _equity = switch_state.get("daily_return_calculation") or {}
            if _equity:
                st.markdown("**Equity Check**")
                _eq1, _eq2, _eq3, _eq4 = st.columns(4)
                _eq1.metric("Ledger return", _equity.get("net_daily_return"), delta=_equity.get("calculation_source"))
                _eq2.metric("Broker equity", _equity.get("current_equity"), delta=_equity.get("equity_ratio_return"))
                _eq3.metric("Start equity", _equity.get("starting_equity"), delta=f"tol={_equity.get('equity_tolerance_pct')}")
                _eq4.metric("Retry/Block", _equity.get("equity_check_attempts"), delta=_equity.get("blocked_reason"))
                _snap = _equity.get("account_snapshot") or {}
                st.caption(
                    f"snapshot_at={_snap.get('as_of')} source={_snap.get('source')} "
                    f"cash={_snap.get('cash')} holdings={_snap.get('holdings_market_value')} "
                    f"grace={_equity.get('settlement_grace_active')} rebased={_equity.get('baseline_rebased')}"
                )

if switch_state.get("mode") == "mock" and switch_state.get("stopped") and "일 누적 손실" in (switch_state.get("stopped_reason") or ""):
    mock_override = st.checkbox(
        "모의계좌 손실제한 무시하고 계속 테스트",
        value=switch_state.get("allow_mock_loss_override", False), key="hynix_switch_mock_override",
    )
    if mock_override != switch_state.get("allow_mock_loss_override"):
        switch_state = set_control(allow_mock_loss_override=mock_override)
        if mock_override:
            switch_state["stopped"] = False
            switch_state["stopped_reason"] = None
            save_state_atomic(switch_state)

if switch_state.get("stopped"):
    st.error(f"🛑 {switch_state.get('stopped_reason')}")
if switch_state.get("residual_position_error"):
    st.error("🔴 전일 포지션이 청산되지 않고 남아 있었습니다 — 프로그램 오류 가능성. 원인 확인이 필요합니다.")
if switch_state.get("position_conflict"):
    st.error("🔴 000660과 0197X0을 동시에 보유 중입니다 — 포지션 동기화 필요, 신규매수가 차단됩니다.")
if switch_state.get("critical_alert"):
    st.error(f"🔴 CRITICAL: {switch_state.get('critical_alert')}")

with st.expander("🩺 REAL 게이트 / 계좌 / 종목코드 진단", expanded=(switch_state.get("mode") == "real")):
    _diag_col1, _diag_col2 = st.columns(2)
    with _diag_col1:
        try:
            import subprocess as _sp
            _git_sha = _sp.check_output(
                ["git", "rev-parse", "--short", "HEAD"], cwd=_PROJECT_ROOT, timeout=5,
            ).decode().strip()
        except Exception:
            _git_sha = "조회 실패"
        st.markdown(f"**Git commit SHA**: `{_git_sha}`")
        try:
            from app.config import _CONFIG_PATH as _CFG_PATH
            st.markdown(f"**config.yaml 경로**: `{_CFG_PATH}`")
        except Exception:
            pass
        st.markdown(f"**현재 broker mode**: `{switch_state.get('mode', 'mock')}`")
    with _diag_col2:
        try:
            from app.config import get_kis_account_config
            _acc_mode = switch_state.get("mode", "mock")
            _acc_cfg = get_kis_account_config(_acc_mode)
            st.markdown(f"**계좌(마스킹)**: `{_acc_cfg.get('masked_account', '')}`")
            st.markdown(f"**계좌 환경변수 source**: `{_acc_cfg.get('account_source', '')}`")
            if _acc_cfg.get("account_conflict"):
                st.error(f"🔴 계좌 환경변수 충돌: {', '.join(_acc_cfg.get('account_conflict_vars', []))}")
        except Exception as exc:
            st.warning(f"계좌 설정 조회 실패: {exc}")

    st.markdown("---")
    st.markdown("**REAL 게이트 항목별 상태**")
    try:
        _gate = cfg.enhanced_real_gate_status(current_mode=switch_state.get("mode", "mock"))
        _gate_checks = _gate.get("checks", {})
        for _k, _v in _gate_checks.items():
            st.markdown(f"{'✅' if _v else '❌'} `{_k}`")
        if _gate.get("blocking_reasons"):
            st.error("차단 사유: " + ", ".join(_gate["blocking_reasons"]))
        else:
            st.success("REAL 게이트 통과 상태입니다(그 외 실전모드 활성화/킬스위치 조건도 함께 확인하세요).")
    except Exception as exc:
        st.warning(f"REAL 게이트 조회 실패: {exc}")

    st.markdown("---")
    st.markdown("**종목코드 검증(현재가+종목명 조회, PDNO에 그대로 전달)**")
    if st.button("000660 / 0197X0 검증 실행", key="hynix_verify_symbols"):
        try:
            from app.trading.kis_client import create_kis_client, verify_symbol
            from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL, INVERSE_NAME
            _verify_mode = switch_state.get("mode", "mock") if switch_state.get("mode") in ("mock", "real") else "mock"
            _verify_client = create_kis_client(_verify_mode)
            if _verify_client is None:
                st.error(f"{_verify_mode} KIS 클라이언트 초기화 실패 — 검증 불가")
            else:
                for _sym, _expected_name in (("000660", "SK하이닉스"), (INVERSE_SYMBOL, INVERSE_NAME)):
                    _res = verify_symbol(_verify_client, _sym, expected_name_substr=_expected_name)
                    if _res["verified"]:
                        st.success(f"✅ {_sym}: 현재가 {_res['current_price']:,.0f}원, 종목명 `{_res['name']}`")
                    else:
                        st.error(f"❌ {_sym}: 검증 실패 — {_res.get('error')}")
        except Exception as exc:
            st.error(f"종목코드 검증 실패: {exc}")

if st.button("🔄 환경설정 다시 읽기", key="hynix_reload_runtime_config"):
    try:
        from app.config import reload_runtime_configuration
        reload_runtime_configuration()
        st.success("환경설정(.env/config.yaml)을 다시 읽고 브로커/토큰 캐시를 초기화했습니다.")
    except Exception as exc:
        st.error(f"환경설정 재로드 실패: {exc}")

switch_run_clicked = st.button("Enhanced 사이클 1회 수동 실행", key="hynix_switch_run_once")

if st.button("🔍 Broker Debug Panel", key="hynix_broker_debug_panel"):
    from app.trading.hynix_position_common import HynixPositionManager
    from app.services.hynix_switch_state import _state_path
    from app.trading.dynamic_exit_watcher import is_watcher_running

    _dbg_mode = switch_state.get("mode", "mock")
    _dbg_error = None
    _dbg_broker = None
    try:
        from app.config import get_config
        from app.trading.broker_factory import create_broker
        if _dbg_mode == "mock":
            _dbg_broker = create_broker(get_config(), mode="mock")
        else:
            _dbg_cfg = get_config()
            _dbg_broker = create_broker(
                _dbg_cfg, mode="real", confirm_text=_dbg_cfg.full_auto_real_confirm_text(),
                runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
            )
    except Exception as exc:
        _dbg_error = str(exc)

    with st.expander("🔍 Broker Debug Panel", expanded=True):
        st.markdown(f"**현재 모드**: `{_dbg_mode}`")
        st.markdown(f"**State file path**: `{_state_path(_dbg_mode)}`")
        if _dbg_error or _dbg_broker is None:
            st.error(f"브로커 초기화 실패: {_dbg_error}")
        else:
            st.markdown(f"**Broker type**: `{type(_dbg_broker).__name__}`")
            try:
                _dbg_cash = _dbg_broker.get_buyable_cash()
            except Exception as exc:
                _dbg_cash = f"조회 실패: {exc}"
            st.markdown(f"**Broker cash**: {_dbg_cash}")
            try:
                _dbg_positions_raw = _dbg_broker.get_positions()
            except Exception as exc:
                _dbg_positions_raw = f"조회 실패: {exc}"
            st.markdown("**Broker positions raw**:")
            st.json([
                {"symbol": p.symbol, "name": p.name, "quantity": p.quantity, "avg_price": p.avg_price}
                for p in _dbg_positions_raw
            ] if isinstance(_dbg_positions_raw, list) else str(_dbg_positions_raw))

            _dbg_pm = HynixPositionManager(_dbg_broker, mode=_dbg_mode)
            _dbg_pm.sync(force=True)
            st.markdown("**PositionManager current_position**:")
            st.json(_dbg_pm.current_position)

            st.markdown(f"**Executed orders count**: {_dbg_pm.trade_count if hasattr(_dbg_broker, 'get_executed_order_count') else 'N/A(브로커가 카운터 미지원)'}")

        st.markdown("**State current_position**:")
        st.json(switch_state.get("position") or {})

        _dbg_ui_position = None
        _dbg_cycle_result = st.session_state.get("hynix_switch_cycle_result")
        if _dbg_cycle_result and not _dbg_cycle_result.get("skipped"):
            _dbg_ui_position = (_dbg_cycle_result.get("position_manager") or {}).get("position")
        st.markdown("**UI displayed position**:")
        st.json(_dbg_ui_position or {"info": "사이클 미실행 — 표시할 UI 포지션 없음"})

        if switch_state.get("last_order_id"):
            st.markdown(
                f"**오늘 마지막 주문**: order_id=`{switch_state.get('last_order_id')}`, "
                f"action=`{switch_state.get('last_action')}`, time=`{switch_state.get('last_trade_time')}`"
            )
        else:
            st.markdown("**오늘 마지막 주문**: 없음")
        st.markdown(
            f"**전체 마지막 주문**: order_id=`{switch_state.get('all_time_last_order_id') or '없음'}`, "
            f"action=`{switch_state.get('all_time_last_action') or '—'}`, "
            f"time=`{switch_state.get('all_time_last_trade_time') or '—'}`"
        )
        st.markdown("**Pending orders**: 없음 (이 시스템은 동기식 즉시체결 구조이며 비동기 대기주문을 갖지 않음)")
        st.markdown(f"**Last sync time(이 패널 조회 시각)**: {datetime.now().isoformat()}")
        st.markdown(f"**Liquidation done**: {switch_state.get('liquidation_done')}")
        st.markdown(f"**Position sync status**: `{switch_state.get('position_sync_status') or 'UNKNOWN'}`")
        if switch_state.get("position_sync_error"):
            st.markdown(f"**Position sync error**: `{switch_state.get('position_sync_error')}`")
        _acct_snap = switch_state.get("last_account_equity_snapshot") or {}
        if _acct_snap:
            st.markdown("**Account equity snapshot**:")
            st.json({
                "as_of": _acct_snap.get("as_of"),
                "source": _acct_snap.get("source"),
                "ok": _acct_snap.get("ok"),
                "cash": _acct_snap.get("cash"),
                "holdings_market_value": _acct_snap.get("holdings_market_value"),
                "current_equity": _acct_snap.get("current_equity"),
                "error": _acct_snap.get("error"),
                "positions": _acct_snap.get("positions") or [],
            })
        st.markdown(f"**Stop loss mode**: {switch_state.get('stop_loss_mode')}")
        st.markdown(
            f"**Dynamic Exit status**: 감시스레드={'실행중' if is_watcher_running() else '정지'}, "
            f"최근 판단={switch_state.get('dynamic_exit_last_decision')}"
        )
        st.markdown(f"**최근 오류 메시지**: {switch_state.get('critical_alert') or '없음'}")

        st.markdown("---")
        st.markdown("**Micron 실시간 데이터 원인 진단**")
        for _mu_label, _mu_filename in (("1분봉", "MU_1min.csv"), ("3분봉", "MU_3min.csv")):
            _mu_path = Path(_PROJECT_ROOT) / "data" / "micron" / _mu_filename
            if not _mu_path.exists():
                st.markdown(f"- {_mu_label}(`{_mu_filename}`): :red[파일 없음] — 수집기가 아직 한 번도 데이터를 쓰지 못함")
                continue
            try:
                import pandas as pd

                _mu_df = pd.read_csv(_mu_path)
                if _mu_df.empty or "datetime" not in _mu_df.columns:
                    st.markdown(f"- {_mu_label}(`{_mu_filename}`): :red[비어있거나 datetime 컬럼 없음]")
                    continue
                _mu_last = pd.to_datetime(_mu_df["datetime"], errors="coerce").dropna().iloc[-1]
                _mu_age_min = (datetime.now() - _mu_last.to_pydatetime().replace(tzinfo=None)).total_seconds() / 60
                _mu_color = "green" if _mu_age_min <= 20 else "red"
                st.markdown(
                    f"- {_mu_label}(`{_mu_filename}`): 마지막 캔들 `{_mu_last}` "
                    f"(:{_mu_color}[{_mu_age_min:.1f}분 전], 신선도 기준 20분)"
                )
            except Exception as exc:
                st.markdown(f"- {_mu_label}(`{_mu_filename}`): :red[읽기 실패] — {exc}")
        st.caption(
            "1분/3분 점수가 '—'로 보이는 이유: 위 두 파일이 모두 20분보다 오래됐거나 없으면 "
            "5분→15분 리샘플, 그다음 세션점수, 그다음 mu_extended_hours 순으로 폴백하고 "
            "그것도 실패하면 중립값 50을 사용합니다(위 '마이크론 데이터 상세' 참고)."
        )

# ── 백그라운드 자동매매 사이클 상태 (브라우저 세션과 무관하게 서버에서 계속 도는 스레드) ──
st.subheader("⚙️ 백그라운드 자동매매 사이클")
_cyc_status = get_cycle_status()
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.metric("auto_trade_enabled", "YES" if switch_state.get("auto_trade_on") else "NO")
with c2:
    st.metric("cycle_thread_alive", "🟢 YES" if _cyc_status["cycle_thread_alive"] else "🔴 NO")
with c3:
    st.metric("cycle_count_today", _cyc_status["cycle_count_today"])
with c4:
    st.metric("restart_count", _cyc_status["restart_count"])
c5, c6, c7 = st.columns(3)
with c5:
    st.markdown(f"**last_cycle_started_at**: `{_cyc_status['last_cycle_started_at'] or '—'}`")
with c6:
    st.markdown(f"**last_cycle_completed_at**: `{_cyc_status['last_cycle_completed_at'] or '—'}`")
with c7:
    st.markdown(f"**next_cycle_at**: `{_cyc_status['next_cycle_at'] or '—'}`")
st.markdown(f"**last_cycle_result**: `{_cyc_status['last_cycle_result_summary'] or '아직 사이클 미실행'}`")
if not _cyc_status["cycle_thread_alive"]:
    st.error("🔴 백그라운드 사이클 스레드가 죽어있습니다 — 다음 페이지 새로고침 시 자동 재시작됩니다.")

_manual_cycle_result = None
if switch_run_clicked:
    # 주의: auto_on이 True라고 해서 여기서도 매 rerun마다 전체 사이클을 다시 돌리지 않는다.
    # 백그라운드 스레드(HynixAutoTradeCycle, 3분 주기)가 auto_on=True인 동안 이미 계속
    # 사이클을 실행하고 있으므로, 페이지가 auto_on일 때도 매번 동일한(네트워크 수집 포함)
    # 무거운 작업을 중복 실행하면 KIS API 타임아웃/상태파일 쓰기 경합이 겹쳐 응답이 크게
    # 느려진다(2026-07-10 실측). "수동 실행" 버튼을 눌렀을 때만 이 페이지 요청 스레드에서
    # 직접 실행하고, 그 외에는 아래 fallback으로 백그라운드 스레드가 저장한 최신 결과만 읽는다.
    with st.spinner("점수 계산 및 자동매매 사이클 실행 중..."):
        _manual_cycle_result = update_hynix_auto_trade_loop(mode=switch_state.get("mode"))
        st.session_state["hynix_switch_cycle_result"] = _manual_cycle_result

# 방금 수동 실행한 결과가 없으면(=이번 rerun이 버튼 클릭이 아니면), session_state의
# 과거 값을 재사용하지 않고 항상 백그라운드 스레드가 마지막으로 저장한 최신 state 기준
# pipeline_trace를 사용한다 — session_state를 그대로 쓰면 마지막 클릭 시점 스냅샷에
# 고정되어 이후 백그라운드 스레드가 갱신한 최신 결과가 화면에 반영되지 않는다.
cycle_result = _manual_cycle_result
if not cycle_result and switch_state.get("last_pipeline_trace") is not None:
    cycle_result = {
        "skipped": False, "computed_at": switch_state.get("last_cycle_computed_at"),
        "mode": switch_state.get("mode"), "new_entry_allowed": is_new_entry_allowed(datetime.now()),
        "hynix_current_price": switch_state.get("last_hynix_price"),
        "inverse_current_price": switch_state.get("last_inverse_price"),
        "enhanced_result": switch_state.get("last_enhanced_result") or {},
        "decision": switch_state.get("last_decision") or {},
        "orders_this_cycle": [],
        "state": switch_state,
        "position_manager": {"position": switch_state.get("position"), "position_conflict": switch_state.get("position_conflict")},
        "pipeline_trace": switch_state.get("last_pipeline_trace"),
        "cycle_ai_shadow_result": switch_state.get("last_cycle_ai_result"),
    }

if not cycle_result:
    st.info("'Enhanced 사이클 1회 수동 실행' 버튼을 누르거나 자동매매를 ON으로 설정하세요.")
elif cycle_result.get("skipped"):
    st.warning(f"이번 사이클은 실행되지 않았습니다: {cycle_result.get('reason')}")
else:
    enh = cycle_result.get("enhanced_result", {})
    decision = cycle_result.get("decision", {})
    state_now = cycle_result.get("state", {})
    # 보유종목/거래횟수는 Broker가 유일한 source of truth다 — position_manager(브로커 sync
    # 직후 결과)만 신뢰하고, state는 entry_time 등 우리쪽 부가 기록을 위한 캐시로만 참고한다.
    pm_cache = cycle_result.get("position_manager") or {}
    position = pm_cache.get("position") or {}
    position_entry_time = (state_now.get("position") or {}).get("entry_time")
    if pm_cache.get("position_conflict"):
        st.error("🔴 000660과 0197X0을 동시에 보유 중입니다 — 포지션 동기화 필요, 신규매수가 차단됩니다.")

    now_badge_cols = st.columns(4)
    with now_badge_cols[0]:
        st.metric("당일매매 모드", "ON (오버나이트 보유 금지)")
    with now_badge_cols[1]:
        st.metric("강제청산 예정 시각", "15:15")
    with now_badge_cols[2]:
        st.metric("14:50 이후 신규매수 금지", "예" if not cycle_result.get("new_entry_allowed") else "아니오")
    with now_badge_cols[3]:
        st.metric("15:15 강제청산 완료", "예" if state_now.get("liquidation_done") else "아니오")

    if position_entry_time and position.get("symbol"):
        try:
            held_minutes = (datetime.fromisoformat(cycle_result["computed_at"]) - datetime.fromisoformat(position_entry_time)).total_seconds() / 60
            st.caption(f"현재 포지션 보유 시간: {held_minutes:.0f}분")
        except Exception:
            pass

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("감시 종목", "SK하이닉스(000660) / SOL 인버스2X(0197X0)")
    with m2:
        st.metric("보유 종목", position.get("name") or "없음", delta=(f"{position.get('quantity')}주" if position.get("quantity") else None))
    with m3:
        st.metric("하이닉스 현재가", f"{cycle_result.get('hynix_current_price'):,.0f}원" if cycle_result.get("hynix_current_price") else "—")
    with m4:
        inv_label = f"{cycle_result.get('inverse_current_price'):,.0f}원" if cycle_result.get("inverse_current_price") else "—"
        st.metric("인버스(0197X0) 현재가", inv_label, delta=("가격 갱신 실패" if enh.get("inverse_price_stale") else None))

    s1, s2, s3, s4, s5 = st.columns(5)
    with s1:
        st.metric("기존 예측점수", f"{_num(enh.get('base_prediction_score')):.1f}")
    micron_detail = enh.get("micron_detail", {}) or {}
    _m1 = micron_detail.get("micron_1min_score")
    _m3 = micron_detail.get("micron_3min_score")
    with s2:
        st.metric(
            "마이크론 실시간점수", f"{_num(enh.get('existing_micron_score')):.1f}",
            delta=f"1분:{_m1 if _m1 is not None else '—'} 3분:{_m3 if _m3 is not None else '—'}",
        )
    with s3:
        st.metric("하이닉스 기술점수", f"{_num(enh.get('hynix_technical_score')):.1f}")
    with s4:
        st.metric("장중 모멘텀점수", f"{_num(enh.get('intraday_momentum_score')):.1f}")
    with s5:
        st.metric("인버스 압력점수", f"{_num(enh.get('inverse_pressure_score')):.1f}")

    with st.expander("마이크론 데이터 상세(fallback 체인)"):
        st.markdown(
            f"- micron_1min_score: {_m1 if _m1 is not None else '—'}\n"
            f"- micron_3min_score: {_m3 if _m3 is not None else '—'}\n"
            f"- micron_fallback_used: {micron_detail.get('micron_fallback_used')}\n"
            f"- micron_data_status: {micron_detail.get('micron_data_status', '—')}\n"
            f"- micron_age_minutes: {enh.get('micron_age_minutes')}\n"
            f"- live_order_weight: {enh.get('micron_live_order_weight')}\n"
            f"- raw_existing_micron_score: {enh.get('raw_existing_micron_score')}\n"
            f"- micron_last_update_time: {micron_detail.get('micron_last_update_time') or '—'}\n"
            f"- source: {micron_detail.get('source', '—')}"
        )
        _contrib = enh.get("score_contributions") or []
        if _contrib:
            import pandas as _pd
            st.dataframe(_pd.DataFrame(_contrib), use_container_width=True, hide_index=True)
        _micron_warnings = micron_detail.get("warnings") or []
        if _micron_warnings:
            st.markdown("**왜 1분/3분 점수가 비어있는지(폴백 단계별 사유)**:")
            for _w in _micron_warnings:
                st.markdown(f"- {_w}")
        else:
            st.markdown(":green[1분/3분 실시간 데이터 정상 사용 중 — 폴백 없음]")

    # ── Micron Proxy Prediction 패널 ──────────────────────────────────────
    # 주의: "Micron futures"라는 단일종목 선물은 존재하지 않는다. 아래 점수는
    # 반드시 "SOX semiconductor futures proxy"/"Nasdaq futures proxy"로 표기한다
    # (실제 CME 선물 체결가가 아니라 SOXX/SOX·NQ=F/QQQ 등 ETF·지수 proxy 기반 추정치).
    st.subheader("🔬 Micron Proxy Prediction")

    # 주의: 이 패널은 더 이상 자체적으로 새 네트워크 수집(collect_and_predict)을 하지 않는다.
    # 예전에는 여기서 MicronProxyPredictionEngine().collect_and_predict()를 매 렌더링마다
    # (최대 60초 캐시) 별도로 호출했는데, 이미 위쪽 enhanced_result가 같은 사이클에서
    # collect_all()로 수집한 market_data를 그대로 재사용하면 되는 것을 중복 수집하고
    # 있었다 — KIS API 타임아웃/부하가 겹쳐 화면이 크게 느려지는 원인 중 하나였다
    # (2026-07-10 실측). 순수 계산 함수만 호출하므로 네트워크 호출이 전혀 없다.
    _mpp = None
    _market_data_for_micron = enh.get("market_data") or {}
    # state 파일에서 재구성한 결과(백그라운드 스레드 fallback 렌더링)는 DataFrame이
    # JSON 저장 과정에서 문자열로 직렬화되어 있어 재계산할 수 없다 — 이 경우는 오류가
    # 아니라 "다음 수동 실행/사이클에서 다시 채워짐" 정상 상태이므로 조용히 건너뛴다.
    _df1_probe = (_market_data_for_micron.get("hynix_minute") or {}).get("df_1min")
    if _market_data_for_micron and not isinstance(_df1_probe, str):
        try:
            from app.models.micron_proxy_prediction import compute_effective_micron_score_from_market_data

            _mpp = compute_effective_micron_score_from_market_data(_market_data_for_micron)
        except Exception as _mpp_exc:
            _mpp = None
            st.warning(f"Micron Proxy Prediction 계산 실패(무해 — 기존 예측 파이프라인은 계속 동작): {_mpp_exc}")
    elif isinstance(_df1_probe, str):
        st.info("이번 렌더링은 저장된 state 기준(백그라운드 스레드 결과)이라 Micron Proxy 재계산에 필요한 원본 데이터가 없습니다 — '수동 실행' 또는 다음 자동 사이클에서 갱신됩니다.")

    # 이번 렌더링에서 재계산이 안 됐어도(원본 데이터 없음/재계산 실패), 백그라운드
    # 사이클이 매번 state에 저장해 둔 마지막 성공 스냅샷 + 경과시간을 표시한다 —
    # 빈 화면 대신 "마지막으로 알려진 값"을 보여주는 것이 사용자에게 더 유용하다.
    if not _mpp:
        _mpp_snap = state_now.get("last_micron_proxy_snapshot") or {}
        if _mpp_snap.get("calculated_at"):
            try:
                _snap_age_min = (datetime.now() - datetime.fromisoformat(_mpp_snap["calculated_at"])).total_seconds() / 60
            except Exception:
                _snap_age_min = None
            sn1, sn2, sn3, sn4 = st.columns(4)
            with sn1:
                _rm = _mpp_snap.get("real_micron_score")
                st.metric("Real Micron Score(마지막값)", f"{_rm:.1f}" if _rm is not None else "—")
            with sn2:
                _sy = _mpp_snap.get("synthetic_micron_score")
                st.metric("Synthetic Micron Score(마지막값)", f"{_sy:.1f}" if _sy is not None else "—")
            with sn3:
                _ef = _mpp_snap.get("effective_micron_score")
                st.metric(
                    "Effective Micron Score(마지막값)", f"{_ef:.1f}" if _ef is not None else "—",
                    delta=(f"{_snap_age_min:.1f}분 전 계산" if _snap_age_min is not None else None),
                )
            with sn4:
                st.metric("Score Source(마지막값)", _mpp_snap.get("score_source") or "—")
            st.caption(
                f"Confidence(마지막값): {_num(_mpp_snap.get('confidence')):.0f} · "
                f"계산시각: {_mpp_snap.get('calculated_at')}"
            )
        else:
            st.info("아직 저장된 Micron Proxy 계산 결과가 없습니다 — 다음 사이클에서 채워집니다.")

    if _mpp:
        p1, p2, p3, p4 = st.columns(4)
        with p1:
            st.metric("Micron 세션", _mpp.get("micron_session", "—"))
        with p2:
            _age = _mpp.get("real_micron_age_minutes")
            st.metric(
                "실제 MU 마지막가", f"{_mpp.get('real_micron_price'):,.2f}" if _mpp.get("real_micron_price") else "—",
                delta=(f"{_age:.1f}분 경과" if _age is not None else None),
            )
        with p3:
            st.metric("Effective Micron Score", f"{_num(_mpp.get('effective_micron_score')):.1f}")
        with p4:
            st.metric("데이터 Confidence", f"{_num(_mpp.get('micron_data_confidence')):.0f}")

        q1, q2, q3, q4, q5 = st.columns(5)
        with q1:
            st.metric("Real Micron Score", f"{_mpp.get('real_micron_score'):.1f}" if _mpp.get("real_micron_score") is not None else "—")
        with q2:
            st.metric("Overnight Micron Score", f"{_mpp.get('overnight_micron_score'):.1f}" if _mpp.get("overnight_micron_score") is not None else "—")
        with q3:
            st.metric("Micron 최근추세 점수", f"{_num(_mpp.get('micron_recent_trend_score')):.1f}")
        with q4:
            st.metric("SOX semiconductor futures proxy", f"{_num(_mpp.get('sox_futures_score')):.1f}")
        with q5:
            st.metric("Nasdaq futures proxy", f"{_num(_mpp.get('nasdaq_futures_score')):.1f}")

        r1, r2, r3 = st.columns(3)
        with r1:
            st.metric("미국 반도체 Proxy Basket", f"{_num(_mpp.get('us_semiconductor_proxy_score')):.1f}")
        with r2:
            st.metric("한국 반도체 확인점수", f"{_num(_mpp.get('korea_semiconductor_confirmation_score')):.1f}")
        with r3:
            st.metric("Synthetic Micron Score", f"{_num(_mpp.get('synthetic_micron_score')):.1f}")

        with st.expander("Micron Proxy Prediction 상세(Source/가중치/경고)"):
            st.markdown(
                f"- micron_score_source: **{_mpp.get('micron_score_source', '—')}**\n"
                f"- micron_session: {_mpp.get('micron_session', '—')}\n"
                f"- 실제 MU 마지막 체결시각: {_mpp.get('real_micron_last_time') or '—'}\n"
                f"- timestamp: {_mpp.get('timestamp', '—')}"
            )
            _session_info = _mpp.get("session_info") or {}
            if _session_info:
                st.markdown(f"- 세션 판정 근거: {_session_info.get('reason', '—')}")
            _reco = None
            try:
                from app.models.micron_proxy_prediction import load_micron_proxy_weight_recommendation

                _reco = load_micron_proxy_weight_recommendation()
            except Exception:
                _reco = None
            if _reco:
                if _reco.get("skipped"):
                    st.markdown(f"- Lead-Lag 추천 가중치: 생략됨 ({_reco.get('reason')})")
                else:
                    st.markdown(f"- Lead-Lag 추천 가중치(표본 {_reco.get('sample_size')}건): `{_reco.get('recommended_weights')}`")
            else:
                st.markdown("- Lead-Lag 추천 가중치: 아직 없음(최소 500표본 누적 전)")
            _mpp_warnings = _mpp.get("warnings") or []
            if _mpp_warnings:
                st.markdown("**경고**:")
                for _w in _mpp_warnings:
                    st.markdown(f"- {_w}")
            else:
                st.markdown(":green[경고 없음]")

    # ── 현재 실제 주문을 지배하는 전략 명시 ────────────────────────────────────
    # 실제 원장(ledger)의 마지막 주문 signal_source를 그대로 보여준다 — 화면에 "적용됨"
    # 이라고 미리 단정하지 않고, 실제로 기록된 값을 근거로 표시한다(2026-07-13 사용자
    # 검증: Prediction V2가 SHADOW인데 적용됐다고 표시하면 안 된다는 요구 반영).
    _active_enabled_now = bool(state_now.get("active_strategy_enabled", False))
    _adaptive_fusion_enabled_now = bool(state_now.get("adaptive_fusion_enabled", False))
    _last_fd = state_now.get("last_final_execution_decision") or {}
    _last_signal_source = _last_fd.get("signal_source")
    if not _active_enabled_now:
        _governing_strategy = "ENHANCED_LEGACY"
    elif not _adaptive_fusion_enabled_now:
        _governing_strategy = "ACTIVE_FUSION"
    else:
        _governing_strategy = _last_signal_source or "ACTIVE_ONLY(대기)"
    st.info(
        f"🎯 **현재 실제 mock 주문을 지배하는 전략: `{_governing_strategy}`** — 실제 체결 담당.  \n"
        + (
            f"최근 체결 signal_source: `{_last_signal_source}`  \n" if (_adaptive_fusion_enabled_now and _last_signal_source) else ""
        )
        + "`CYCLE_AI`는 항상 SHADOW MODE — 계산·기록만 하며, `PREDICTION_V2`는 Adaptive Fusion이 켜져있고 "
        "성능검증을 통과(ADVISORY/LIVE_VALIDATED)했을 때만 실제 주문에 반영됩니다(SHADOW 상태면 반영되지 않습니다).",
        icon="🎯",
    )

    # ── Signal → Order 파이프라인: 예측 신호와 실제 체결을 단계별로 분리 표시 ──
    st.subheader("🔬 Signal → Order 파이프라인")
    trace = cycle_result.get("pipeline_trace") or {}
    stopped_stage = trace.get("stopped_stage")

    def _stage_line(stage_key: str, label: str, value, extra: str = "") -> None:
        if value is True:
            color, text = "green", "YES"
        elif value is False:
            color, text = "red", "NO"
        else:
            color, text = "gray", "N/A"
        marker = " ⛔ **여기서 멈췄습니다**" if stage_key == stopped_stage else ""
        suffix = f" — {extra}" if extra else ""
        st.markdown(f"**{label}**: :{color}[{text}]{suffix}{marker}")

    pp1, pp2 = st.columns(2)
    with pp1:
        st.markdown(f"**Prediction Signal**: :blue[{trace.get('prediction_signal', 'HOLD')}]")
        _stage_line("entry_approved", "Entry Approved", trace.get("entry_approved"), trace.get("entry_approved_reason") or "")
        _stage_line("risk_manager", "Risk Manager 승인", trace.get("risk_manager_ok"), trace.get("risk_manager_reason") or "")
        _stage_line("order_sent", "Order Sent", trace.get("order_sent"))
    with pp2:
        _stage_line("broker_executed", "Broker Executed", trace.get("broker_executed"))
        _stage_line("position_confirmed", "Position Confirmed", trace.get("position_confirmed"))
        _stage_line("ui_synced", "UI Synced", trace.get("ui_synced"))
        st.markdown(f"**Trade Counter**: {trace.get('trade_counter', 0)}")

    if stopped_stage:
        st.error(
            f"🔴 이번 사이클: 신호는 **{trace.get('prediction_signal')}**였지만 **{stopped_stage}** 단계에서 "
            f"멈춰 실제 체결로 이어지지 않았습니다. blocking_reason: {trace.get('blocking_reason') or '—'}"
        )
    elif trace.get("prediction_signal") != "HOLD":
        st.success("🟢 신호부터 UI 반영까지 전 단계 정상 완료.")

    # ── 🔄 Cycle & Turning Point AI (SHADOW MODE — 실제 주문에 영향 없음) ──────
    st.subheader("🔄 Cycle & Turning Point AI")
    st.caption(
        "SHADOW MODE — 아래 결과는 기존 Enhanced 자동매매 판단과 나란히 계산·기록만 되며, "
        "실제 주문에는 아직 연결되어 있지 않습니다(최소 5거래일 검증 후 별도 승인 시 연결 예정)."
    )
    shadow = cycle_result.get("cycle_ai_shadow_result")
    if not shadow:
        st.info("Cycle AI 결과 없음(다음 사이클에 계산됩니다).")
    else:
        cyc = shadow.get("cycle") or {}
        mom = cyc.get("momentum") or {}
        tp = cyc.get("turning_point") or {}
        prob = shadow.get("probability") or {}
        dv2 = shadow.get("decision_v2") or {}

        cc1, cc2, cc3, cc4 = st.columns(4)
        with cc1:
            st.metric("Current Cycle Phase", cyc.get("cycle_phase", "—"))
        with cc2:
            st.metric("Previous Cycle Phase", cyc.get("previous_cycle_phase") or "—")
        with cc3:
            _started = cyc.get("phase_started_at")
            st.metric("Phase Started At", _started.split("T")[-1][:8] if _started else "—")
        with cc4:
            _dur = cyc.get("phase_duration_seconds") or 0
            st.metric("Phase Duration", f"{_dur // 60}분 {_dur % 60}초")

        cc5, cc6, cc7, cc8 = st.columns(4)
        with cc5:
            st.metric("Momentum Velocity", f"{mom.get('raw_velocity_3', 0):.3f}" if mom.get("raw_velocity_3") is not None else "—")
        with cc6:
            st.metric("Momentum Accel Up", f"{_num(mom.get('momentum_acceleration_up')):.1f}")
        with cc7:
            st.metric("Momentum Accel Down", f"{_num(mom.get('momentum_acceleration_down')):.1f}")
        with cc8:
            st.metric("Cycle Confidence", f"{_num(cyc.get('cycle_confidence')):.1f}")

        cc9, cc10, cc11 = st.columns(3)
        with cc9:
            st.metric("Turning Up (3/5/10m)", f"{_num(tp.get('up_turn_probability_3m')):.0f} / {_num(tp.get('up_turn_probability_5m')):.0f} / {_num(tp.get('up_turn_probability_10m')):.0f}")
        with cc10:
            st.metric("Turning Down (3/5/10m)", f"{_num(tp.get('down_turn_probability_3m')):.0f} / {_num(tp.get('down_turn_probability_5m')):.0f} / {_num(tp.get('down_turn_probability_10m')):.0f}")
        with cc11:
            st.metric("Cycle Entry Score", f"{max((cyc.get('entry_scores') or {}).values(), default=0):.1f}")

        cc12, cc13, cc14 = st.columns(3)
        with cc12:
            st.metric("Recommended Symbol", cyc.get("recommended_symbol") or "—")
        with cc13:
            st.metric("Recommended Position", f"{_num(cyc.get('recommended_position_pct')):.0f}%")
        with cc14:
            st.metric("Combined Shadow Action", shadow.get("combined_shadow_action", "HOLD"))

        st.markdown(
            f"**Prediction AI V2**: BUY {_num(prob.get('buy_probability')):.0f}% / "
            f"SELL {_num(prob.get('sell_probability')):.0f}% / HOLD {_num(prob.get('hold_probability')):.0f}% "
            f"→ **{dv2.get('final_action_v2', 'HOLD')}** (Adaptive Threshold: {_num(dv2.get('buy_threshold'), 65):.0f}%)"
        )
        if cyc.get("blocking_reason"):
            st.warning(f"Blocking Reason: {cyc['blocking_reason']}")
        with st.expander("Cycle AI 상세(사유/전환이력)"):
            for r in cyc.get("reasons") or []:
                st.markdown(f"- {r}")
            history = cyc.get("transition_history") or []
            if history:
                st.markdown("**State Transition History(최근 10건)**")
                for h in history[-10:]:
                    st.markdown(f"- {h.get('at', '')}: {h.get('from') or '—'} → {h.get('to')}")

    # ── 📈 Big Trend Holding AI(장중 큰 추세 추종 + 수익보호 + 반전확인청산) ───
    st.subheader("📈 Big Trend Holding AI")
    _bt_enabled = bool(state_now.get("big_trend_holding_enabled", False))
    st.caption(
        "실제 청산을 지배함" if _bt_enabled else
        "SHADOW MODE — 계산·기록만 하며 실제 청산은 기존 Dynamic Exit AI(고정 tp/sl 프로필)가 담당합니다."
    )
    _bt = state_now.get("last_big_trend_result")
    if not _bt:
        st.info("보유 포지션이 없거나 아직 계산되지 않았습니다(포지션 보유 중에만 계산됩니다).")
    else:
        bt1, bt2, bt3, bt4 = st.columns(4)
        with bt1:
            st.metric("Dominant Direction", _bt.get("dominant_direction", "—"))
        with bt2:
            st.metric("Trend Regime", _bt.get("trend_regime", "—"))
        with bt3:
            st.metric("Trend Strength", f"{_num(_bt.get('trend_strength_score')):.0f}")
        with bt4:
            st.metric("Trend Persistence", f"{_num(_bt.get('trend_persistence_score')):.0f}")

        _regime_state = _bt.get("regime_state") or {}
        _regime_duration = None
        _started = _regime_state.get("regime_started_at")
        if _started:
            try:
                from datetime import datetime as _dt2
                _regime_duration = (datetime.now() - _dt2.fromisoformat(_started)).total_seconds() / 60.0
            except Exception:
                _regime_duration = None
        bt5, bt6, bt7 = st.columns(3)
        with bt5:
            st.metric("Regime 유지시간", f"{_regime_duration:.0f}분" if _regime_duration is not None else "—")
        with bt6:
            st.metric("Regime 전환횟수(오늘)", _regime_state.get("transition_count", 0))
        with bt7:
            _min_hold = _bt.get("min_hold_minutes")
            st.metric("목표 보유시간", f"{_min_hold[0]:.0f}~{_min_hold[1]:.0f}분" if _min_hold else "—")

        bt8, bt9, bt10 = st.columns(3)
        with bt8:
            st.metric("Hold Confidence", f"{_num(_bt.get('hold_confidence')):.0f}")
        with bt9:
            st.metric("Exit Confidence", f"{_bt.get('exit_confidence') or 0:.0f}" if _bt.get("exit_confidence") is not None else "—")
        with bt10:
            st.metric("Reversal 확인(개)", f"{(_bt.get('reversal_confirmation') or {}).get('matched', 0)}/9")

        bt11, bt12, bt13, bt14 = st.columns(4)
        with bt11:
            st.metric("Peak Net Return", f"{_num(_bt.get('peak_net_return_pct')):.2f}%")
        with bt12:
            st.metric("Current Net Return", f"{_num(_bt.get('net_return_pct')):.2f}%")
        with bt13:
            _giveback = bte.compute_profit_giveback_pct(_bt.get("peak_net_return_pct", 0), _bt.get("net_return_pct", 0))
            st.metric("Profit Giveback", f"{_giveback:.2f}%p")
        with bt14:
            _floor = _bt.get("current_profit_lock_pct")
            st.metric("Profit Lock Floor", f"{_floor:.2f}%" if _floor is not None else "—")

        bt15, bt16, bt17 = st.columns(3)
        with bt15:
            st.metric("현재 익절 방식", f"+{_bt.get('reasons', [''])[0][:30]}" if _bt.get("final_hold_action") in ("TAKE_PROFIT_25", "TAKE_PROFIT_50") else "부분/전량청산 대기 중")
        with bt16:
            st.metric("현재 손절 방식(effective_sl_pct)", f"{_num(_bt.get('effective_sl_pct')):.2f}%")
        with bt17:
            st.metric("현재 Trailing 폭", f"{_bt.get('trailing_pct') or 0:.2f}%" if _bt.get("trailing_pct") is not None else "—")

        bt18, bt19 = st.columns(2)
        with bt18:
            st.metric("Remaining Position %", f"{_num(_bt.get('max_position_pct')):.0f}%")
        with bt19:
            st.metric("Final Hold Action", _bt.get("final_hold_action") or "—")

        _transition = _bt.get("regime_transition_action") or {}
        if _transition.get("action") not in (None, "NONE"):
            st.warning(
                f"🔁 Regime 전환 대응: **{_transition['action']}**"
                + (f" (비중 {_transition.get('reduce_ratio', 0)*100:.0f}% 축소)" if _transition.get("reduce_ratio") else "")
            )

        with st.expander("Big Trend 판단 사유"):
            for r in _bt.get("reasons") or []:
                st.markdown(f"- {r}")

    st.subheader(f"개선된 최종점수: {_num(enh.get('enhanced_score')):.1f}/100 → 최종 판단: {decision.get('final_action', 'HOLD')}")
    with st.expander("판단 사유 Top5", expanded=True):
        for i, reason in enumerate(enh.get("reason_top5") or [], start=1):
            st.markdown(f"{i}. {reason}")
        if decision.get("reasons"):
            st.markdown("**판단 세부:**")
            for r in decision["reasons"]:
                st.markdown(f"- {r}")

    # ── 📊 거래 성과 통계(실행 원장 기준, TEST 주문 제외) ─────────────────────
    st.subheader("📊 거래 성과 통계 (원장 기준, TEST 주문 제외)")
    try:
        from app.services.hynix_execution_ledger import (
            compute_performance_stats, compute_trade_counters, calculate_daily_net_pnl_from_ledger,
        )

        def _safe_float(value, default=0.0):
            try:
                if value is None:
                    return default
                return float(value)
            except (TypeError, ValueError):
                return default

        def _fmt_krw(value) -> str:
            return f"{_safe_float(value):,.0f}원"

        def _fmt_minutes(value) -> str:
            return f"{_safe_float(value):.0f}분" if value is not None else "—"

        def _fmt_pct(value) -> str:
            return f"{_safe_float(value):.0f}%" if value is not None else "—"

        _today_str = datetime.now().strftime("%Y%m%d")
        _stats = compute_performance_stats(_today_str)
        _counters = compute_trade_counters(_today_str)
        _cost_stats = calculate_daily_net_pnl_from_ledger(_today_str)

        # 체결 수는 반드시 매수/매도/총/왕복거래를 명확히 분리해 표시한다 — "총 체결 수 1 /
        # 오늘 거래 횟수 0"처럼 서로 다른 소스(원장 vs pm_cache)를 섞어 보여주면 의미가
        # 혼동된다(2026-07-13 사용자 리포트). 아래 4개는 모두 동일한 원장(ledger) 기준이다.
        cnt1, cnt2, cnt3, cnt4 = st.columns(4)
        with cnt1:
            st.metric("오늘 매수 체결 수", _cost_stats.get("buy_fill_count", _counters.get("buy_fill_count", 0)))
        with cnt2:
            st.metric("오늘 매도 체결 수", _cost_stats.get("sell_fill_count", _counters.get("sell_fill_count", 0)))
        with cnt3:
            st.metric("오늘 총 체결 수", _cost_stats.get("operating_trade_count", _counters.get("live_order_count", 0)))
        with cnt4:
            st.metric("오늘 완료 왕복거래 수", _cost_stats.get("round_trip_count", _counters.get("round_trip_count", 0)))

        st3, st4, st5 = st.columns(3)
        with st3:
            st.metric("평균 보유시간", _fmt_minutes(_stats.get("avg_holding_minutes")))
        with st4:
            st.metric("승률", _fmt_pct(_stats.get("win_rate")))
        with st5:
            _pf = _stats.get("profit_factor")
            _pf_float = _safe_float(_pf, default=None)
            st.metric("Profit Factor", f"{_pf_float:.2f}" if _pf_float is not None and _pf_float != float("inf") else ("∞" if _pf == float("inf") else "—"))

        st6, st7, st8 = st.columns(3)
        with st6:
            st.metric("누적 실현손익(오늘)", _fmt_krw(_stats.get("cumulative_realized_pnl")))
        with st7:
            st.metric("최대 장중 손실(DD)", _fmt_krw(_stats.get("max_intraday_drawdown_krw")) if _stats.get("max_intraday_drawdown_krw") is not None else "—")
        with st8:
            st.metric("TEST 주문 수(통계 제외됨)", _counters.get("test_order_count", 0))

        # 거래비용 breakdown — Gross/Net 실현손익과 총 매수·매도수수료/거래세/슬리피지를
        # 분리 표시한다(2026-07-13 사용자 요청). 비용이 반영되지 않은 과거 데이터가
        # 섞여 있으면(수수료 합계가 전부 0) 그 사실도 그대로 드러난다 — 임의로 숨기지 않는다.
        st.markdown("**거래비용 (Gross → Net)**")
        cost1, cost2, cost3 = st.columns(3)
        with cost1:
            st.metric("Gross 실현손익", _fmt_krw(_cost_stats.get("gross_realized_pnl")))
        with cost2:
            st.metric("총 거래비용", _fmt_krw(_cost_stats.get("total_trading_cost")))
        with cost3:
            st.metric("Net 실현손익", _fmt_krw(_cost_stats.get("net_realized_pnl")))
        cost4, cost5, cost6 = st.columns(3)
        with cost4:
            st.metric("총 매수수수료", _fmt_krw(_cost_stats.get("total_buy_fee")))
        with cost5:
            st.metric("총 매도수수료", _fmt_krw(_cost_stats.get("total_sell_fee")))
        with cost6:
            st.metric("총 거래세", _fmt_krw(_cost_stats.get("total_transaction_tax")))
        st.metric("총 슬리피지", _fmt_krw(_cost_stats.get("total_slippage_cost")))
        if _safe_float(_cost_stats.get("total_commission")) == 0.0 and _safe_float(_cost_stats.get("total_transaction_tax")) == 0.0 and _safe_float(_cost_stats.get("gross_realized_pnl")) != 0.0:
            st.caption("⚠️ 이 거래들은 거래비용 엔진 도입 이전에 체결된 기록이라 수수료/세금이 0으로 표시됩니다(과거 데이터).")

        if _stats.get("pnl_by_signal_source"):
            with st.expander("전략별(signal_source) / 종목별 손익"):
                st.markdown("**전략별 손익:**")
                for src, pnl in _stats.get("pnl_by_signal_source", {}).items():
                    st.markdown(f"- {src}: {_fmt_krw(pnl)}")
                st.markdown("**종목별 손익:**")
                for sym, pnl in _stats.get("pnl_by_symbol", {}).items():
                    st.markdown(f"- {sym}: {_fmt_krw(pnl)}")
    except Exception as _stats_exc:
        st.caption(f"거래 성과 통계 계산 실패(무해): {_stats_exc}")

    # 보유 종목 없음이면(브로커 기준) 최근매수가/미실현손익/손절·익절 기준가는 과거 값을
    # 그대로 표시하지 않고 "—"로 처리한다 — 그대로 두면 "이미 종료된 매매의 진입가"가
    # "지금 보유 중인 포지션"처럼 보여 혼동을 준다. 거래내역 표(아래 dataframe)에는 과거
    # 기록이 그대로 남아 있어도 무방하다.
    _has_position = bool(position.get("symbol")) and (position.get("quantity") or 0) > 0

    # 실현/미실현손익은 모두 NetPnL(매수·매도 수수료 + 거래세 + 슬리피지 차감 후)이 단일
    # Source of Truth다 — "오늘 실현손익(순손익)"은 반드시 net_realized_pnl이어야 하고
    # (2026-07-13 사용자 요청), Gross는 참고용으로 항상 옆에 나란히 표시해 혼동을 막는다.
    try:
        _ledger_pnl_single = _cost_stats
    except NameError:
        from app.services.hynix_execution_ledger import calculate_daily_net_pnl_from_ledger
        _ledger_pnl_single = calculate_daily_net_pnl_from_ledger(datetime.now().strftime("%Y%m%d"))
    _net_realized = _ledger_pnl_single["net_realized_pnl"]
    _gross_realized = _ledger_pnl_single["gross_realized_pnl"]
    _total_cost_today = _ledger_pnl_single["total_trading_cost"]
    _net_return_pct = _ledger_pnl_single["net_daily_return_pct"]
    _gross_return_pct = (
        _gross_realized / _ledger_pnl_single["starting_equity"] * 100.0
        if _ledger_pnl_single["starting_equity"] else 0.0
    )

    st.markdown("**오늘 실현손익 (Gross → Net, 단일 기준)**")
    t1, t2, t3 = st.columns(3)
    with t1:
        st.metric("Gross 실현손익", f"{_gross_realized:,.0f}원")
    with t2:
        st.metric("총 거래비용(수수료+세금+슬리피지)", f"{_total_cost_today:,.0f}원")
    with t3:
        st.metric("Net 실현손익(오늘 실현손익·순손익)", f"{_net_realized:,.0f}원")

    t4, t5, t6 = st.columns(3)
    with t4:
        st.metric("Gross 수익률", f"{_gross_return_pct:.4f}%")
    with t5:
        st.metric("Net 수익률 (오늘 수익률)", f"{_net_return_pct:.4f}%")
    with t6:
        _gross_unreal = state_now.get("gross_unrealized_pnl", 0)
        _net_unreal = state_now.get("unrealized_pnl", 0)
        st.metric(
            "현재 미실현손익(순손익)", f"{_net_unreal:,.0f}원" if _has_position else "—",
            delta=(f"Gross {_gross_unreal:,.0f}원" if _has_position else None),
        )
    st.caption(
        "net_daily_return = (net_realized_pnl + net_unrealized_pnl) / starting_equity(당일 시작 자산). "
        "일 손익 리스크 사다리(신규진입 중단/전량청산 판단)도 이 Net 기준 값을 그대로 사용합니다."
    )
    st.caption(
        f"starting_equity={_ledger_pnl_single['starting_equity']:,.0f}원, "
        f"net_realized_pnl={_net_realized:,.2f}원, "
        f"net_daily_return={_net_return_pct:.4f}%"
    )

    if _has_position:
        _cost_breakdown = state_now.get("unrealized_pnl_cost_breakdown") or {}
        if _cost_breakdown:
            with st.expander("거래비용 상세(예상 매도수수료/거래세/슬리피지)"):
                cb1, cb2, cb3, cb4 = st.columns(4)
                with cb1:
                    st.metric("이미 지불한 매수수수료", f"{_cost_breakdown.get('already_paid_buy_fee', 0):,.0f}원")
                with cb2:
                    st.metric("예상 매도수수료", f"{_cost_breakdown.get('estimated_exit_fee', 0):,.0f}원")
                with cb3:
                    st.metric("예상 거래세", f"{_cost_breakdown.get('estimated_exit_tax', 0):,.0f}원")
                with cb4:
                    st.metric("예상 슬리피지", f"{_cost_breakdown.get('estimated_slippage', 0):,.0f}원")

    # 보유 포지션이 있으면 "최근 매수 가격 — / 거래 발생 시각 —"처럼 빈 값을 보여주지
    # 않고, 원장(execution ledger)에서 재구성한 평균매수가/최초진입시각/최근추가매수
    # 시각/총투자금액/포지션비중을 반드시 표시한다(2026-07-13 사용자 리포트).
    _pos_detail: dict = {}
    if _has_position:
        try:
            from app.services.hynix_execution_ledger import compute_current_position_detail

            _pos_detail = compute_current_position_detail(
                position.get("symbol"), total_equity=state_now.get("total_equity"),
            )
        except Exception:
            _pos_detail = {}

    if _has_position and _pos_detail.get("has_position"):
        pd1, pd2, pd3, pd4, pd5 = st.columns(5)
        with pd1:
            _avg = _pos_detail.get("avg_buy_price")
            st.metric("평균 매수가", f"{_avg:,.0f}원" if _avg is not None else "—")
        with pd2:
            _fe = _pos_detail.get("first_entry_time")
            st.metric("최초 진입시각", _fe.split("T")[-1][:8] if _fe else "—")
        with pd3:
            _la = _pos_detail.get("last_add_time")
            st.metric("최근 추가매수 시각", _la.split("T")[-1][:8] if _la else "—")
        with pd4:
            _inv = _pos_detail.get("total_invested_krw")
            st.metric("총 투자금액", f"{_inv:,.0f}원" if _inv is not None else "—")
        with pd5:
            _pct = _pos_detail.get("position_pct")
            st.metric("현재 포지션 비중", f"{_pct:.1f}%" if _pct is not None else "—")
    elif _has_position:
        st.caption(
            "브로커 기준 포지션은 보유 중이나 원장에서 매수 이력을 아직 찾지 못했습니다"
            "(백필 이전 데이터/수동 개입 가능성) — 다음 사이클에서 갱신됩니다."
        )
    else:
        p1, p2 = st.columns(2)
        with p1:
            st.metric("최근 매도 가격", f"{state_now.get('last_sell_price'):,.0f}원" if state_now.get("last_sell_price") else "—")
        with p2:
            st.metric("거래 발생 시각", state_now.get("last_trade_time") or "—")

    sl1, sl2 = st.columns(2)
    _dyn_decision_now = state_now.get("dynamic_exit_last_decision") or {}
    _entry_price_now = position.get("avg_price") or (state_now.get("position") or {}).get("entry_price")
    _sl_pct_now = _dyn_decision_now.get("sl_pct")
    _tp_pct_now = _dyn_decision_now.get("tp_pct")
    with sl1:
        if _has_position and _entry_price_now and _sl_pct_now is not None:
            st.metric("자동손절 기준가", f"{_entry_price_now * (1 - _sl_pct_now / 100):,.0f}원", delta=f"-{_sl_pct_now}%")
        else:
            st.metric("자동손절 기준가", "—")
    with sl2:
        if _has_position and _entry_price_now and _tp_pct_now is not None:
            st.metric("자동익절 기준가", f"{_entry_price_now * (1 + _tp_pct_now / 100):,.0f}원", delta=f"+{_tp_pct_now}%")
        else:
            st.metric("자동익절 기준가", "—")

    # ── 자동 진단 경고: 화면에 보이는 값들 사이의 불일치를 즉시 알린다 ────────
    _diag_warnings: list[str] = []
    if not _has_position and state_now.get("last_action") == "BUY":
        _diag_warnings.append(
            "보유종목 없음인데 마지막 기록된 동작이 BUY입니다 — 강제청산/장외 매도 여부 확인 필요."
        )
    _now_check = datetime.now()
    if _now_check.time() >= dtime_cls(15, 15) and not _has_position and not state_now.get("liquidation_done"):
        _diag_warnings.append("15:15 이후이며 보유종목이 없는데 liquidation_done=False입니다 — 상태 갱신 지연 의심.")
    if pm_cache.get("position_conflict"):
        _diag_warnings.append("브로커에 000660/0197X0을 동시 보유 중입니다(CONFLICT) — 포지션 동기화 필요.")

    # 주의: 이 진단은 반드시 원장(execution ledger) 기준이어야 한다. 과거 이 블록이
    # legacy hynix_auto_trade_log_{date}.csv만 읽었는데, Dynamic Exit AI(1초 감시
    # 스레드)의 매도는 3분 사이클 쪽의 log_trade() 호출 경로를 거치지 않아 그 CSV에는
    # 전혀 기록되지 않는다 — "매수기록은 있는데 매도가 안 보인다"는 오탐 경고와
    # "오늘 거래내역"에 매도가 누락되는 사고(2026-07-13 사용자 리포트)의 원인이었다.
    try:
        from app.services.hynix_execution_ledger import load_ledger as _load_ledger_for_diag

        _ledger_today = _load_ledger_for_diag(datetime.now().strftime("%Y%m%d"))
        if not _ledger_today.empty and "action" in _ledger_today.columns:
            _buy_rows = _ledger_today[(_ledger_today["action"] == "BUY") & (_ledger_today["success"] == True)]  # noqa: E712
            _sell_rows = _ledger_today[(_ledger_today["action"] == "SELL") & (_ledger_today["success"] == True)]  # noqa: E712
            if not _buy_rows.empty:
                _last_buy_ts = _buy_rows.iloc[-1]["timestamp"]
                _last_sell_ts = _sell_rows.iloc[-1]["timestamp"] if not _sell_rows.empty else None
                _buy_is_latest = _last_sell_ts is None or _last_buy_ts > _last_sell_ts
                if _buy_is_latest and not _has_position:
                    _diag_warnings.append("거래원장에 성공한 BUY 기록이 있으나 브로커에 보유 포지션이 없습니다 — 체결 확인 필요.")
                if state_now.get("mode") == "real" and _buy_is_latest and not _has_position:
                    _diag_warnings.append("real 주문원장(BUY)은 있으나 KIS 잔고 증가가 확인되지 않습니다 — 체결 미확인 가능성.")
    except Exception:
        pass

    _diag_dyn_state = load_state()
    _diag_dyn_position = _diag_dyn_state.get("position") or {}
    if _has_position and not _diag_dyn_position.get("symbol"):
        _diag_warnings.append("브로커 기준 보유 포지션이 있으나 Dynamic Exit AI가 이를 인식하지 못하고 있습니다 — 감시 스레드 상태 확인 필요.")

    if _diag_warnings:
        st.error("🔴 자동 진단 경고:\n\n" + "\n".join(f"- {w}" for w in _diag_warnings))

    if cycle_result.get("orders_this_cycle"):
        st.markdown("**이번 사이클 거래 사유**")
        for order in cycle_result["orders_this_cycle"]:
            st.markdown(f"- [{order.get('action')}] {order.get('symbol')} {order.get('quantity')}주 @ {order.get('price')} — {order.get('reason')}")

    # "오늘 거래내역"은 반드시 원장(execution ledger) 기준으로 표시한다 — legacy
    # hynix_auto_trade_log_{date}.csv는 3분 사이클(ENHANCED_LEGACY/ACTIVE_STRATEGY_MOCK)
    # 매수·매도만 기록하고 Dynamic Exit AI(1초 감시)의 매도는 기록하지 않아, 매수만
    # 보이고 매도가 누락되는 문제가 있었다(2026-07-13 사용자 리포트). 원장에는 모든
    # 실행 경로(신규진입/스위칭/레거시 TP·SL/강제청산/Dynamic Exit)가 공통으로 쓴다.
    date_str = datetime.now().strftime("%Y%m%d")
    try:
        from app.services.hynix_execution_ledger import calculate_daily_net_pnl_from_ledger

        _ledger_display_stats = calculate_daily_net_pnl_from_ledger(date_str)
        _ledger_today_display = _ledger_display_stats["trades"]
    except Exception:
        _ledger_today_display = None
        _ledger_display_stats = None

    if _ledger_today_display is not None and not _ledger_today_display.empty:
        st.markdown(
            "**오늘 거래내역 (원장 기준 — 매수/매도/스위칭/Dynamic Exit 전체)**  \n"
            "realized_pnl은 순손익(NetPnL, 수수료·거래세·슬리피지 차감 후)이며, "
            "gross_pnl은 참고용 매수·매도가 차이(수수료 미차감)입니다."
        )
        _ledger_display_cols = [
            "timestamp", "action", "symbol", "executed_qty", "executed_price",
            "gross_pnl", "buy_fee", "sell_fee", "transaction_tax", "slippage_cost", "net_pnl",
            "signal_source", "success", "is_test_order", "order_id",
        ]
        _ledger_display_cols = [c for c in _ledger_display_cols if c in _ledger_today_display.columns]
        _display_df = _ledger_today_display[_ledger_display_cols].sort_values("timestamp").reset_index(drop=True)
        c_raw, c_filtered, c_displayed = st.columns(3)
        with c_raw:
            st.metric("Ledger 원본 행 수", _ledger_display_stats["ledger_raw_row_count"])
        with c_filtered:
            st.metric("필터 후 운영 체결 수", _ledger_display_stats["operating_trade_count"])
        with c_displayed:
            st.metric("화면 표시 행 수", len(_display_df))
        if (
            _ledger_display_stats["ledger_raw_row_count"] != _ledger_display_stats["operating_trade_count"]
            or _ledger_display_stats["operating_trade_count"] != len(_display_df)
        ):
            st.error("거래원장 행 수와 UI 표시 행 수가 불일치합니다.")
        st.dataframe(
            _display_df,
            use_container_width=True,
            hide_index=True,
            height=min(760, max(220, 38 * (len(_display_df) + 1))),
        )
    else:
        st.caption("오늘 원장에 기록된 거래가 아직 없습니다.")

    st.caption(f"최근 업데이트 시각: {cycle_result.get('computed_at')}")
    if cycle_result.get("warnings"):
        with st.expander("경고/알림"):
            for w in cycle_result["warnings"]:
                st.markdown(f"- {w}")

# ─────────────────────────────────────────────────────────────────────────────
# 예측 정확도 · 반자동 가중치 학습
# ─────────────────────────────────────────────────────────────────────────────

st.divider()
st.header("📊 예측 정확도 · 가중치 학습")
st.caption("판단 로그와 실제 가격을 비교해 예측 정확도를 계산하고, 가중치 조정 후보를 추천합니다(자동 반영 없음 — 사람 승인 후에만 반영).")

from app.services.hynix_prediction_tracker import _read_outcome_log_for_dates, compute_accuracy
from app.services.hynix_weight_recommender import load_recommendation, recommend_weight_adjustment
from app.services.hynix_weight_manager import get_active_weights, apply_recommended_weights, reset_weights_to_default

_today_str = datetime.now().strftime("%Y%m%d")
_today_outcomes = _read_outcome_log_for_dates([_today_str])

acc_cols = st.columns(4)
_horizon_labels = [(3, "3분"), (5, "5분"), (10, "10분"), (30, "30분")]
_today_accs = []
for _col, (_h, _label) in zip(acc_cols, _horizon_labels):
    _acc = compute_accuracy(_today_outcomes, _h)
    if _acc is not None:
        _today_accs.append(_acc)
    with _col:
        st.metric(f"최근 {_label} 예측 정확도", f"{_acc:.1f}%" if _acc is not None else "—")

acc_cols2 = st.columns(2)
with acc_cols2[0]:
    st.metric("오늘 예측 성공률(평균)", f"{(sum(_today_accs) / len(_today_accs)):.1f}%" if _today_accs else "—")
with acc_cols2[1]:
    _five_day_acc = None
    _report_path = Path(_PROJECT_ROOT) / "data" / "reports" / "hynix_prediction_daily_report.csv"
    if _report_path.exists():
        try:
            import pandas as pd
            _rep = pd.read_csv(_report_path).tail(5)
            _acc_values = []
            for _c in ["accuracy_3m", "accuracy_5m", "accuracy_10m", "accuracy_30m", "accuracy_close"]:
                if _c in _rep.columns:
                    _acc_values.extend(pd.to_numeric(_rep[_c], errors="coerce").dropna().tolist())
            if _acc_values:
                _five_day_acc = sum(_acc_values) / len(_acc_values)
        except Exception:
            pass
    st.metric("최근 5거래일 예측 성공률(평균)", f"{_five_day_acc:.1f}%" if _five_day_acc is not None else "—")

st.subheader("모델 가중치")
wb1, wb2, wb3, wb4 = st.columns(4)
with wb1:
    show_current_clicked = st.button("현재 가중치 보기", key="hynix_weight_show_current", use_container_width=True)
with wb2:
    show_recommended_clicked = st.button("추천 가중치 보기", key="hynix_weight_show_recommended", use_container_width=True)
with wb3:
    apply_weight_clicked = st.button("추천 가중치 적용", key="hynix_weight_apply", use_container_width=True)
with wb4:
    reset_weight_clicked = st.button("기본값으로 되돌리기", key="hynix_weight_reset", use_container_width=True)

if show_current_clicked:
    st.markdown("**현재 가중치**")
    st.json(get_active_weights())

if show_recommended_clicked:
    st.session_state["hynix_weight_recommendation"] = load_recommendation() or recommend_weight_adjustment()

if apply_weight_clicked:
    apply_result = apply_recommended_weights()
    if apply_result.get("success"):
        st.success(f"가중치 적용 완료: {apply_result.get('weights')}")
    else:
        st.error(apply_result.get("message"))

if reset_weight_clicked:
    reset_result = reset_weights_to_default()
    if reset_result.get("success"):
        st.success(f"기본 가중치로 복원: {reset_result.get('weights')}")
    else:
        st.error(reset_result.get("message"))

_recommendation = st.session_state.get("hynix_weight_recommendation")
if _recommendation:
    st.markdown("**추천 가중치**")
    if _recommendation.get("skipped"):
        st.info(_recommendation.get("reason"))
    else:
        st.json(_recommendation.get("recommended_weights"))
        st.caption(f"추천 사유: {_recommendation.get('reason')}")
        st.caption(f"샘플 수: {_recommendation.get('sample_size')} · 기대 개선치: {_recommendation.get('expected_improvement')}")

_mock_auto_apply = st.checkbox(
    "[실험용] mock 모드에서 추천 가중치 자동 적용",
    value=switch_state.get("weight_auto_apply_enabled", False), key="hynix_weight_auto_apply_toggle",
)
if _mock_auto_apply != switch_state.get("weight_auto_apply_enabled"):
    switch_state["weight_auto_apply_enabled"] = _mock_auto_apply
    save_state_atomic(switch_state)

# ─────────────────────────────────────────────────────────────────────────────
# Dynamic Exit AI (실시간 익절·손절·트레일링·Profit Lock)
# ─────────────────────────────────────────────────────────────────────────────

st.divider()
st.header("🎯 Dynamic Exit AI")
st.caption(
    "자동매매 ON 상태에서 1초 주기 백그라운드 스레드가 보유 포지션의 청산 조건을 실시간으로 판단합니다. "
    "기존 고정 익절 3%/손절 1.5%는 이 엔진이 판단할 수 없을 때의 fallback으로만 사용됩니다."
)

from app.trading.dynamic_exit_watcher import ensure_watcher_running, is_watcher_running
from app.services.hynix_exit_recommender import (
    load_exit_recommendation, load_daily_exit_learning, recommend_exit_parameters,
)

# auto_trade_on 여부와 무관하게 스레드 자체는 항상 살려둔다 — 꺼져있으면 내부에서
# 대기만 하고, 켜지는 순간 다음 틱부터 즉시 반응한다(페이지를 새로 열지 않아도 됨).
ensure_watcher_running()
ensure_cycle_thread_running()
ensure_fast_trend_watcher_running()

st.caption(f"백그라운드 감시 스레드: {'🟢 실행 중' if is_watcher_running() else '🔴 정지(비정상)'}")

# ── 청산 엔진 현재 상태 요약 — "지금 실제로 무엇이 청산을 결정하는가"를 한 곳에서
# 명확히 표시한다(2026-07-13 사용자 요청). 우선순위: 감시 스레드가 죽어있으면
# 레거시 TP/SL fallback이 담당하고(run_tp_sl_if_needed 참고), 살아있으면 Big Trend
# Holding AI가 켜져 있을 때만(mock 전용) 그 결과가 실제 action/ratio를 대체하며,
# 그 외에는 Dynamic Exit AI(고정 프로필 기반)가 담당한다. 초기 손절 안전장치는
# 어느 엔진이 담당하든 항상 최우선 적용된다.
_ee_watcher_alive = is_watcher_running()
_ee_state = load_state()
_ee_bt_on = bool(_ee_state.get("big_trend_holding_enabled", False))
_ee_dyn_decision = _ee_state.get("dynamic_exit_last_decision") or {}
_ee_bt_result = _ee_state.get("last_big_trend_result") or {}

if not _ee_watcher_alive:
    _current_exit_engine = "LEGACY_TP_SL_FALLBACK"
elif _ee_bt_on:
    _current_exit_engine = "BIG_TREND_HOLDING_AI"
else:
    _current_exit_engine = "DYNAMIC_EXIT_AI"

if _current_exit_engine == "BIG_TREND_HOLDING_AI" and _ee_bt_result:
    _ee_sl_val = _ee_bt_result.get("effective_sl_pct")
    effective_sl_policy = f"-{_ee_sl_val:.2f}%(Big Trend 안전장치)" if _ee_sl_val is not None else "—"
    effective_tp_policy = "Regime별 부분익절(25%/50%)+Profit Lock — 고정 %가 아니라 추세 강도에 따라 변동"
    _ee_trail_val = _ee_bt_result.get("trailing_pct")
    trailing_policy = f"{_ee_trail_val:.2f}%(Adaptive Trailing)" if _ee_trail_val is not None else "미발동"
elif _current_exit_engine == "DYNAMIC_EXIT_AI" and _ee_dyn_decision:
    _ee_sl_val = _ee_dyn_decision.get("sl_pct")
    _ee_tp_val = _ee_dyn_decision.get("tp_pct")
    effective_sl_policy = f"-{_ee_sl_val}%" if _ee_sl_val is not None else "—"
    effective_tp_policy = f"+{_ee_tp_val}%" if _ee_tp_val is not None else "—"
    trailing_policy = (
        "ON(발동)" if _ee_dyn_decision.get("trailing_armed")
        else ("대기(미발동)" if _ee_dyn_decision.get("trailing_enabled") else "미사용")
    )
else:
    from app.trading.hynix_switch_position_manager import _load_section, _DEFAULT_RISK

    _ee_legacy_risk = _load_section("risk", _DEFAULT_RISK)
    effective_sl_policy = f"{_ee_legacy_risk['stop_loss_1_pct']}%(50%)/{_ee_legacy_risk['stop_loss_2_pct']}%(전량) — 레거시 고정"
    effective_tp_policy = f"+{_ee_legacy_risk['take_profit_1_pct']}%(50%)/+{_ee_legacy_risk['take_profit_2_pct']}%(전량) — 레거시 고정"
    trailing_policy = "미사용(레거시 TP/SL은 트레일링 없음)"

st.markdown("**🛡️ 청산 엔진 현재 상태 (실제 청산을 지배하는 시스템)**")
ee1, ee2, ee3 = st.columns(3)
with ee1:
    st.metric("current_exit_engine", _current_exit_engine)
with ee2:
    st.metric("dynamic_exit_enabled", "YES" if _ee_watcher_alive else "NO(감시 스레드 정지)")
with ee3:
    st.metric("big_trend_holding_enabled", "YES" if _ee_bt_on else "NO")
ee4, ee5, ee6 = st.columns(3)
with ee4:
    st.metric("effective_tp_policy", effective_tp_policy)
with ee5:
    st.metric("effective_sl_policy", effective_sl_policy)
with ee6:
    st.metric("trailing_policy", trailing_policy)
st.caption(
    "우선순위: 감시 스레드 정지 → LEGACY_TP_SL_FALLBACK 담당 / 스레드 정상 + Big Trend ON(mock 전용) → "
    "BIG_TREND_HOLDING_AI가 실제 action·ratio 대체 / 그 외 → DYNAMIC_EXIT_AI. 초기 손절 안전장치(effective_sl_pct)는 "
    "어느 엔진이 담당하든 토글과 무관하게 항상 최우선 적용됩니다."
)

_dyn_state = load_state()
_dyn_position = _dyn_state.get("position") or {}
_dyn_decision = _dyn_state.get("dynamic_exit_last_decision")

if not _dyn_position.get("symbol") or not _dyn_decision:
    st.info("보유 포지션이 없거나 아직 Dynamic Exit AI 판단 결과가 없습니다.")
else:
    ex1, ex2, ex3, ex4 = st.columns(4)
    with ex1:
        st.metric("현재 TP", f"{_dyn_decision.get('tp_pct')}%")
    with ex2:
        st.metric("현재 SL", f"{_dyn_decision.get('sl_pct')}%")
    with ex3:
        trailing_label = "ON" if _dyn_decision.get("trailing_armed") else ("대기" if _dyn_decision.get("trailing_enabled") else "미사용")
        st.metric("Trailing", trailing_label)
    with ex4:
        lock_floor = _dyn_decision.get("profit_lock_floor_pct")
        st.metric("Profit Lock", f"+{lock_floor:.1f}%" if lock_floor is not None else "미발동")

    ex5, ex6, ex7 = st.columns(3)
    with ex5:
        st.metric("시장유형", _dyn_decision.get("market_type", "—"))
    with ex6:
        _held_minutes = None
        if _dyn_position.get("entry_time"):
            try:
                _held_minutes = (datetime.now() - datetime.fromisoformat(_dyn_position["entry_time"])).total_seconds() / 60
            except Exception:
                pass
        st.metric("보유시간", f"{_held_minutes:.0f}분" if _held_minutes is not None else "—")
    with ex7:
        st.metric("Exit Score", f"{_dyn_decision.get('exit_score', 0):.0f}/100")

    ex8, ex9 = st.columns(2)
    with ex8:
        hp = _dyn_position.get("highest_price")
        st.metric("현재 최고가", f"{hp:,.0f}원" if hp else "—")
    with ex9:
        lp = _dyn_position.get("lowest_price")
        st.metric("현재 최저가", f"{lp:,.0f}원" if lp else "—")

    st.caption(f"판단 사유: {_dyn_decision.get('reason', '—')}")

st.divider()
st.subheader("🛡️ 손절 실행 방식")

from app.trading.hynix_stop_loss_control import (
    STOP_LOSS_MODE_AUTO,
    STOP_LOSS_MODES,
    STOP_LOSS_MODE_LABELS,
    execute_manual_stop_loss,
)
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL

_current_stop_mode = switch_state.get("stop_loss_mode", STOP_LOSS_MODE_AUTO)
_stop_mode_choice = st.radio(
    "손절 방식",
    STOP_LOSS_MODES,
    index=STOP_LOSS_MODES.index(_current_stop_mode) if _current_stop_mode in STOP_LOSS_MODES else 0,
    format_func=lambda m: STOP_LOSS_MODE_LABELS.get(m, m),
    key="hynix_stop_loss_mode_radio",
    horizontal=True,
)
if _stop_mode_choice != _current_stop_mode:
    switch_state["stop_loss_mode"] = _stop_mode_choice
    save_state_atomic(switch_state)
    st.success(f"손절 방식이 '{STOP_LOSS_MODE_LABELS[_stop_mode_choice]}'로 변경되었습니다.")
    st.rerun()

_pending_alert = switch_state.get("pending_manual_stop_loss_alert")
if _pending_alert:
    st.warning(
        f"⚠️ 손절/청산 조건 도달 — 자동매도가 실행되지 않았습니다. "
        f"[{_pending_alert.get('symbol')}] {_pending_alert.get('action', '—')} — "
        f"{_pending_alert.get('reason', '—')} (감지시각: {_pending_alert.get('detected_at', '—')})"
    )

st.caption("아래 버튼은 손절 방식과 무관하게 언제든 즉시 전량 청산을 실행합니다(현재 모드의 실제 계좌/브로커 기준).")
mb1, mb2, mb3 = st.columns(3)
with mb1:
    if st.button("하이닉스 전량 수동손절", key="hynix_manual_sl_hynix", use_container_width=True):
        _sl_result = execute_manual_stop_loss(switch_state.get("mode", "mock"), symbol_filter=HYNIX_SYMBOL)
        (st.success if _sl_result["success"] else st.warning)(_sl_result["message"])
        st.json(_sl_result["results"])
with mb2:
    if st.button("인버스 전량 수동손절", key="hynix_manual_sl_inverse", use_container_width=True):
        _sl_result = execute_manual_stop_loss(switch_state.get("mode", "mock"), symbol_filter=INVERSE_SYMBOL)
        (st.success if _sl_result["success"] else st.warning)(_sl_result["message"])
        st.json(_sl_result["results"])
with mb3:
    if st.button("자동매매 대상 전량 청산", key="hynix_manual_sl_all", use_container_width=True):
        _sl_result = execute_manual_stop_loss(switch_state.get("mode", "mock"), symbol_filter=None)
        (st.success if _sl_result["success"] else st.warning)(_sl_result["message"])
        st.json(_sl_result["results"])

st.markdown("**AI 청산 파라미터 추천 (자동 반영 없음)**")
if st.button("청산 파라미터 추천 새로고침", key="hynix_exit_recommend_refresh"):
    st.session_state["hynix_exit_recommendation"] = recommend_exit_parameters()

_exit_rec = st.session_state.get("hynix_exit_recommendation") or load_exit_recommendation()
if _exit_rec:
    if _exit_rec.get("skipped"):
        st.info(_exit_rec.get("reason"))
    else:
        st.json(_exit_rec.get("recommended"))
        st.caption(_exit_rec.get("reason"))

_daily_learning = load_daily_exit_learning()
if _daily_learning and _daily_learning.get("suggestions"):
    with st.expander("오늘의 청산 조정 제안"):
        for _s in _daily_learning["suggestions"]:
            st.markdown(f"- {_s.get('note')}")
