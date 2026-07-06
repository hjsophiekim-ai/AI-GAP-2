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
