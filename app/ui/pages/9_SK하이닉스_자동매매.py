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

if auto_on or switch_run_clicked:
    with st.spinner("점수 계산 및 자동매매 사이클 실행 중..."):
        st.session_state["hynix_switch_cycle_result"] = update_hynix_auto_trade_loop(mode=switch_state.get("mode"))

cycle_result = st.session_state.get("hynix_switch_cycle_result")

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
    with s2:
        micron_detail = enh.get("micron_detail", {}) or {}
        st.metric("마이크론 실시간점수", f"{enh.get('existing_micron_score', 0):.1f}",
                   delta=f"1분:{micron_detail.get('micron_1min_score')} 3분:{micron_detail.get('micron_3min_score')}")
    with s3:
        st.metric("하이닉스 기술점수", f"{enh.get('hynix_technical_score', 0):.1f}")
    with s4:
        st.metric("장중 모멘텀점수", f"{enh.get('intraday_momentum_score', 0):.1f}")
    with s5:
        st.metric("인버스 압력점수", f"{enh.get('inverse_pressure_score', 0):.1f}")

    st.subheader(f"개선된 최종점수: {enh.get('enhanced_score', 0):.1f}/100 → 최종 판단: {decision.get('final_action', 'HOLD')}")
    with st.expander("판단 사유 Top5", expanded=True):
        for i, reason in enumerate(enh.get("reason_top5") or [], start=1):
            st.markdown(f"{i}. {reason}")
        if decision.get("reasons"):
            st.markdown("**판단 세부:**")
            for r in decision["reasons"]:
                st.markdown(f"- {r}")

    t1, t2, t3, t4 = st.columns(4)
    with t1:
        st.metric("오늘 거래 횟수", pm_cache.get("trade_count", 0))
    with t2:
        st.metric("오늘 실현손익", f"{state_now.get('realized_pnl_today_krw', 0):,.0f}원")
    with t3:
        st.metric("오늘 수익률", f"{state_now.get('realized_pnl_today_pct', 0):.2f}%")
    with t4:
        st.metric("현재 미실현손익", f"{state_now.get('unrealized_pnl', 0):,.0f}원")

    p1, p2, p3 = st.columns(3)
    with p1:
        st.metric("최근 매수 가격", f"{state_now.get('last_buy_price'):,.0f}원" if state_now.get("last_buy_price") else "—")
    with p2:
        st.metric("최근 매도 가격", f"{state_now.get('last_sell_price'):,.0f}원" if state_now.get("last_sell_price") else "—")
    with p3:
        st.metric("거래 발생 시각", state_now.get("last_trade_time") or "—")

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

if switch_state.get("auto_trade_on"):
    ensure_watcher_running()

st.caption(f"백그라운드 감시 스레드: {'🟢 실행 중' if is_watcher_running() else '⚪ 정지(자동매매 OFF)'}")

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
