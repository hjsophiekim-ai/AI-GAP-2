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
from app.services.hynix_auto_trade_scheduler import ensure_cycle_thread_running, get_status as get_cycle_status

try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=180_000, key="hynix_switch_autorefresh")
except Exception:
    pass

switch_state = load_state()

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
        real_gate_ok = cfg.full_auto_real_confirm_ok()
        st.caption(
            f"REAL 완전자동 게이트: {'✅ 충족' if real_gate_ok else '❌ 미충족'} "
            "(.env의 FULL_AUTO_REAL_CONFIRM_TEXT + config.yaml safety.enable_real_trading 필요 — 기존 완전자동 게이트 재사용)"
        )

if auto_on != switch_state.get("auto_trade_on") or switch_mode != switch_state.get("mode"):
    switch_state = set_control(auto_trade_on=auto_on, mode=switch_mode)

# ── 현재 실행 모드 배너 — 항상 눈에 띄게 표시(REAL이면 빨간 경고) ────────────
_active_switch_mode = switch_state.get("mode", "mock")
if _active_switch_mode == "real":
    _real_gate_ok_banner = cfg.full_auto_real_confirm_ok()
    st.error(
        f"🔴🔴🔴 **REAL 모드 — 실제 계좌로 주문이 나갈 수 있습니다.** "
        f"REAL 완전자동 게이트: {'✅ 충족(주문 가능)' if _real_gate_ok_banner else '❌ 미충족 — 주문 최종 차단됨'}",
        icon="🚨",
    )
else:
    st.success("🟢 MOCK 모드 — DryRunBroker(로컬 시뮬레이션)로만 동작 중. 실제 계좌/주문 없음.")

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
        if st.button("Mock 계좌 초기화", key="hynix_switch_mock_reset", use_container_width=True):
            switch_state = reset_mock_account(budget_krw=budget_input)
            st.success(f"Mock 계좌를 {budget_input:,.0f}원으로 초기화했습니다.")
    st.caption(
        "Mock 모드는 KIS 모의투자 서버(계좌 승인/장시간 제약)와 무관하게 여기서 설정한 예산으로 "
        "로컬에서 완전히 자동으로 동작합니다(DryRunBroker). Real 모드만 실제 KIS 계좌를 사용합니다."
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

switch_run_clicked = st.button("Enhanced 사이클 1회 수동 실행", key="hynix_switch_run_once")

if st.button("🔍 Broker Debug Panel", key="hynix_broker_debug_panel"):
    from app.trading.hynix_position_common import HynixPositionManager
    from app.services.hynix_switch_state import _state_path
    from app.trading.dynamic_exit_watcher import is_watcher_running

    _dbg_mode = switch_state.get("mode", "mock")
    _dbg_error = None
    _dbg_broker = None
    try:
        if _dbg_mode == "mock":
            from app.trading.dry_run_broker import DryRunBroker

            _dbg_broker = DryRunBroker(initial_balance=float(switch_state.get("mock_budget_krw", 10_000_000.0)))
        else:
            from app.config import get_config
            from app.trading.broker_factory import create_broker

            _dbg_broker = create_broker(
                get_config(), mode="real",
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

if auto_on or switch_run_clicked:
    with st.spinner("점수 계산 및 자동매매 사이클 실행 중..."):
        st.session_state["hynix_switch_cycle_result"] = update_hynix_auto_trade_loop(mode=switch_state.get("mode"))

# 세션에 수동/방금 실행분이 없으면, 백그라운드 스레드가 마지막으로 저장한 state 기준
# pipeline_trace를 사용한다 — "사이클 미실행"이 더 이상 상시로 뜨지 않도록 한다.
cycle_result = st.session_state.get("hynix_switch_cycle_result")
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
        st.metric("기존 예측점수", f"{enh.get('base_prediction_score', 0):.1f}")
    micron_detail = enh.get("micron_detail", {}) or {}
    _m1 = micron_detail.get("micron_1min_score")
    _m3 = micron_detail.get("micron_3min_score")
    with s2:
        st.metric(
            "마이크론 실시간점수", f"{enh.get('existing_micron_score', 0):.1f}",
            delta=f"1분:{_m1 if _m1 is not None else '—'} 3분:{_m3 if _m3 is not None else '—'}",
        )
    with s3:
        st.metric("하이닉스 기술점수", f"{enh.get('hynix_technical_score', 0):.1f}")
    with s4:
        st.metric("장중 모멘텀점수", f"{enh.get('intraday_momentum_score', 0):.1f}")
    with s5:
        st.metric("인버스 압력점수", f"{enh.get('inverse_pressure_score', 0):.1f}")

    with st.expander("마이크론 데이터 상세(fallback 체인)"):
        st.markdown(
            f"- micron_1min_score: {_m1 if _m1 is not None else '—'}\n"
            f"- micron_3min_score: {_m3 if _m3 is not None else '—'}\n"
            f"- micron_fallback_used: {micron_detail.get('micron_fallback_used')}\n"
            f"- micron_data_status: {micron_detail.get('micron_data_status', '—')}\n"
            f"- micron_last_update_time: {micron_detail.get('micron_last_update_time') or '—'}\n"
            f"- source: {micron_detail.get('source', '—')}"
        )
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

    @st.cache_data(ttl=60, show_spinner=False)
    def _run_micron_proxy_prediction(mode: str):
        from app.models.micron_proxy_prediction import MicronProxyPredictionEngine

        return MicronProxyPredictionEngine().collect_and_predict(mode=mode)

    try:
        _mpp = _run_micron_proxy_prediction(cfg.mode if cfg.mode in ("mock", "real") else None)
    except Exception as _mpp_exc:
        _mpp = None
        st.warning(f"Micron Proxy Prediction 계산 실패(무해 — 기존 예측 파이프라인은 계속 동작): {_mpp_exc}")

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
            st.metric("Effective Micron Score", f"{_mpp.get('effective_micron_score', 0):.1f}")
        with p4:
            st.metric("데이터 Confidence", f"{_mpp.get('micron_data_confidence', 0):.0f}")

        q1, q2, q3, q4, q5 = st.columns(5)
        with q1:
            st.metric("Real Micron Score", f"{_mpp.get('real_micron_score'):.1f}" if _mpp.get("real_micron_score") is not None else "—")
        with q2:
            st.metric("Overnight Micron Score", f"{_mpp.get('overnight_micron_score'):.1f}" if _mpp.get("overnight_micron_score") is not None else "—")
        with q3:
            st.metric("Micron 최근추세 점수", f"{_mpp.get('micron_recent_trend_score', 0):.1f}")
        with q4:
            st.metric("SOX semiconductor futures proxy", f"{_mpp.get('sox_futures_score', 0):.1f}")
        with q5:
            st.metric("Nasdaq futures proxy", f"{_mpp.get('nasdaq_futures_score', 0):.1f}")

        r1, r2, r3 = st.columns(3)
        with r1:
            st.metric("미국 반도체 Proxy Basket", f"{_mpp.get('us_semiconductor_proxy_score', 0):.1f}")
        with r2:
            st.metric("한국 반도체 확인점수", f"{_mpp.get('korea_semiconductor_confirmation_score', 0):.1f}")
        with r3:
            st.metric("Synthetic Micron Score", f"{_mpp.get('synthetic_micron_score', 0):.1f}")

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

    st.subheader(f"개선된 최종점수: {enh.get('enhanced_score', 0):.1f}/100 → 최종 판단: {decision.get('final_action', 'HOLD')}")
    with st.expander("판단 사유 Top5", expanded=True):
        for i, reason in enumerate(enh.get("reason_top5") or [], start=1):
            st.markdown(f"{i}. {reason}")
        if decision.get("reasons"):
            st.markdown("**판단 세부:**")
            for r in decision["reasons"]:
                st.markdown(f"- {r}")

    # 보유 종목 없음이면(브로커 기준) 최근매수가/미실현손익/손절·익절 기준가는 과거 값을
    # 그대로 표시하지 않고 "—"로 처리한다 — 그대로 두면 "이미 종료된 매매의 진입가"가
    # "지금 보유 중인 포지션"처럼 보여 혼동을 준다. 거래내역 표(아래 dataframe)에는 과거
    # 기록이 그대로 남아 있어도 무방하다.
    _has_position = bool(position.get("symbol")) and (position.get("quantity") or 0) > 0

    t1, t2, t3, t4 = st.columns(4)
    with t1:
        st.metric("오늘 거래 횟수", pm_cache.get("trade_count", 0))
    with t2:
        st.metric("오늘 실현손익", f"{state_now.get('realized_pnl_today_krw', 0):,.0f}원")
    with t3:
        st.metric("오늘 수익률", f"{state_now.get('realized_pnl_today_pct', 0):.2f}%")
    with t4:
        st.metric("현재 미실현손익", f"{state_now.get('unrealized_pnl', 0):,.0f}원" if _has_position else "—")

    p1, p2, p3 = st.columns(3)
    with p1:
        st.metric(
            "최근 매수 가격",
            f"{state_now.get('last_buy_price'):,.0f}원" if (_has_position and state_now.get("last_buy_price")) else "—",
        )
    with p2:
        st.metric("최근 매도 가격", f"{state_now.get('last_sell_price'):,.0f}원" if state_now.get("last_sell_price") else "—")
    with p3:
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

    _today_trade_log_path = Path(_PROJECT_ROOT) / "data" / "logs" / f"hynix_auto_trade_log_{datetime.now().strftime('%Y%m%d')}.csv"
    if _today_trade_log_path.exists():
        try:
            import pandas as pd

            _log_rows = pd.read_csv(_today_trade_log_path)
            if "action" in _log_rows.columns and "success" in _log_rows.columns:
                _buy_rows = _log_rows[_log_rows["action"].astype(str).str.upper().str.startswith("BUY") & (_log_rows["success"] == True)]  # noqa: E712
                _sell_rows = _log_rows[_log_rows["action"].astype(str).str.upper().str.startswith("SELL") & (_log_rows["success"] == True)]  # noqa: E712
                if not _buy_rows.empty:
                    _last_buy_ts = _buy_rows.iloc[-1]["timestamp"]
                    _last_sell_ts = _sell_rows.iloc[-1]["timestamp"] if not _sell_rows.empty else None
                    _buy_is_latest = _last_sell_ts is None or str(_last_buy_ts) > str(_last_sell_ts)
                    if _buy_is_latest and not _has_position:
                        _diag_warnings.append("거래로그에 성공한 BUY 기록이 있으나 브로커에 보유 포지션이 없습니다 — 체결 확인 필요.")
                    if state_now.get("mode") == "real" and _buy_is_latest and not _has_position:
                        _diag_warnings.append("real 주문로그(BUY)는 있으나 KIS 잔고 증가가 확인되지 않습니다 — 체결 미확인 가능성.")
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

    date_str = datetime.now().strftime("%Y%m%d")
    trade_log_path = Path(_PROJECT_ROOT) / "data" / "logs" / f"hynix_auto_trade_log_{date_str}.csv"
    if trade_log_path.exists():
        st.markdown("**오늘 거래내역**")
        try:
            import pandas as pd
            st.dataframe(pd.read_csv(trade_log_path).tail(20), use_container_width=True)
        except Exception:
            pass

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

st.caption(f"백그라운드 감시 스레드: {'🟢 실행 중' if is_watcher_running() else '🔴 정지(비정상)'}")

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
