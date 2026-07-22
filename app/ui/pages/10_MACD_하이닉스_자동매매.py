"""
10_MACD_하이닉스_자동매매.py

완전히 독립된 MACD Histogram(3분봉) 하이닉스 ETF 자동매매 UI.
기존 Enhanced/WOC/Early/Active/Fusion/Regime/Prediction 페이지·엔진을 호출하지 않는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pandas as pd
import streamlit as st

from app.ui.auth_gate import require_login

require_login()

from app.config import get_config, get_kis_account_config, mask_account  # noqa: E402
from app.trading import macd_hynix_order_manager as om  # noqa: E402
from app.trading import macd_hynix_worker as worker  # noqa: E402
from app.trading.macd_hynix_strategy import (  # noqa: E402
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SIGNAL_SYMBOL,
)

try:
    from streamlit_autorefresh import st_autorefresh

    st_autorefresh(interval=5000, key="macd_hynix_refresh")
except Exception:
    pass

st.set_page_config(page_title="MACD 하이닉스 자동매매", layout="wide")
st.title("MACD 하이닉스 자동매매")
st.caption(
    "Strategy B · 000660 완성 3분봉 MACD Histogram만 사용 · "
    "ETF(0193T0/0197X0)는 주문·손익 전용 · 기존 Enhanced 모듈과 완전 분리"
)

cfg = get_config()
state = om.load_state()
wstatus = worker.ensure_worker_running()

# ── Controls ──────────────────────────────────────────────────────────────
st.subheader("계좌 / 제어")
c1, c2, c3, c4 = st.columns([1.2, 1.2, 1, 1])
with c1:
    mode = st.radio(
        "계좌 모드",
        ["mock", "real"],
        index=0 if str(state.get("mode") or "mock") != "real" else 1,
        horizontal=True,
        key="macd_mode",
    )
with c2:
    budget = st.number_input(
        "투자예산 (원)",
        min_value=100_000,
        max_value=500_000_000,
        value=int(state.get("budget") or 10_000_000),
        step=100_000,
        key="macd_budget",
    )
with c3:
    acct = get_kis_account_config(mode)
    masked = acct.get("masked_account") or mask_account(acct.get("account_no", ""))
    st.metric("계좌", masked or "(미설정)")
with c4:
    can_order = bool(state.get("auto_trade_on")) and mode == str(state.get("mode"))
    st.metric("주문 가능", "YES" if can_order else "NO")

real_confirm_ok = False
if mode == "real":
    st.error("REAL(실전) 모드 — 확인 문구 입력 후에만 시작 가능")
    expected = str(cfg.real_confirm_text() or "live")
    confirm_in = st.text_input("REAL 확인 문구", type="password", key="macd_real_confirm")
    real_toggle = st.checkbox("REAL 주문 활성화", key="macd_real_toggle")
    real_confirm_ok = bool(real_toggle and confirm_in == expected)
    if not real_confirm_ok:
        st.warning(f"확인 문구가 일치해야 합니다 (설정값 길이={len(expected)})")
else:
    st.info("MOCK 모드 (기본값) — KIS 모의투자 계좌")

b1, b2, b3 = st.columns(3)
with b1:
    if st.button("자동매매 시작", type="primary", use_container_width=True):
        res = worker.start_auto_trade(
            mode=mode,
            budget=float(budget),
            real_confirm_ok=real_confirm_ok if mode == "real" else False,
            masked_account=masked,
        )
        if res.get("ok"):
            st.success("MACD 자동매매 시작")
            st.rerun()
        else:
            st.error(res.get("message") or "시작 실패")
with b2:
    if st.button("자동매매 중지", use_container_width=True):
        worker.stop_auto_trade("user_stop")
        st.warning("중지됨")
        st.rerun()
with b3:
    if st.button("즉시 전량청산", use_container_width=True):
        worker.request_force_liquidate()
        st.warning("전량청산 요청됨 — Worker가 처리합니다")
        st.rerun()

old_on, old_src = om.read_old_auto_trade_on()
legacy_dbg = om.legacy_auto_trade_truth(force_disk=True)
if old_on:
    st.error(
        f"LEGACY_STRATEGY_ACTIVE — Enhanced auto_trade_on=True "
        f"(truth=`{legacy_dbg.get('truth_helper')}`, path=`{legacy_dbg.get('enhanced_save_path')}`). "
        f"MACD 시작이 차단됩니다."
    )
if state.get("auto_trade_on"):
    st.success(f"MACD 자동매매 ON · mode={state.get('mode')} · budget={int(state.get('budget') or 0):,}")
else:
    st.info("MACD 자동매매 OFF")

st.caption(
    "상호배타: MACD Start는 Enhanced와 동일하게 "
    "`hynix_switch_state.load_state()`의 `auto_trade_on`만 본다 "
    f"(OFF write=`{legacy_dbg.get('enhanced_save_path')}` · "
    f"common=`{legacy_dbg.get('enhanced_common_path')}` · "
    f"AI_GAP_DATA_DIR=`{legacy_dbg.get('AI_GAP_DATA_DIR')}`). "
    "mutex 파일 존재만으로는 차단하지 않습니다."
)

# Clear status split — worker alive ≠ strategy running
st.subheader("상태 분리")
s1, s2, s3, s4, s5, s6 = st.columns(6)
s1.metric("scheduler_alive", "YES" if (state.get("scheduler_alive") or (state.get("worker") or {}).get("alive") or wstatus.get("alive")) else "NO")
s2.metric("strategy_enabled", "YES" if state.get("strategy_enabled") or state.get("auto_trade_on") else "NO")
s3.metric("market_data_active", "YES" if state.get("market_data_active") else "NO")
s4.metric("signal_calculation_active", "YES" if state.get("signal_calculation_active") else "NO")
s5.metric("order_execution_enabled", "YES" if state.get("order_execution_enabled") else "NO")
s6.metric("primary_block_reason", str(state.get("primary_block_reason") or "-")[:40])
if state.get("auto_trade_on"):
    st.caption("전략 실행 중 (strategy_enabled=YES). Worker alive만으로는 자동매매 실행으로 표시하지 않습니다.")
else:
    st.caption("전략 OFF — Worker가 살아 있어도 자동매매 실행 중이 아닙니다.")

# ── Diagnostics ───────────────────────────────────────────────────────────
st.subheader("현재 진단")
light = state.get("display_direction") or DIR_HOLD
if light == DIR_UP:
    light_label = "🔴 빨간불 (UP_RED)"
elif light == DIR_DOWN:
    light_label = "🔵 파란불 (DOWN_BLUE)"
else:
    light_label = "⚪ HOLD"

prices = state.get("prices") or {}
macd = state.get("macd") or {}
pos = state.get("position") or {}

m1, m2, m3, m4 = st.columns(4)
m1.metric("하이닉스 000660", f"{prices.get('hynix') or '-'}")
m2.metric("KODEX 0193T0", f"{prices.get('long') or '-'}")
m3.metric("SOL 0197X0", f"{prices.get('inverse') or '-'}")
m4.metric("MACD 상태", light_label)

d1, d2, d3 = st.columns(3)
d1.write(f"**MACD**: {macd.get('macd')}")
d2.write(f"**Signal**: {macd.get('signal')}")
d3.write(f"**Histogram**: {macd.get('hist')}")
st.write(
    f"최근 3 Histogram: `{macd.get('hist_last3')}` · "
    f"변화량: `{macd.get('hist_deltas')}` · reason=`{macd.get('reason')}`"
)
quote_errors = state.get("quote_errors") or []
if quote_errors:
    st.error("시세 조회 오류:")
    for err in quote_errors:
        st.code(
            f"api={err.get('api_function')} symbol={err.get('symbol')} "
            f"code={err.get('response_code')} retries={err.get('retry_count')} "
            f"msg={err.get('error_message')}"
        )

held_sym = pos.get("symbol") or "-"
qty = int(pos.get("quantity") or 0)
avg = float(pos.get("avg_price") or 0)
cur = None
if held_sym == LONG_SYMBOL:
    cur = prices.get("long")
elif held_sym == INVERSE_SYMBOL:
    cur = prices.get("inverse")
upnl = None
if cur and avg and qty:
    upnl = (float(cur) - avg) * qty
st.write(
    f"**보유**: {held_sym} · 수량 {qty} · 평단 {avg:,.0f} · "
    f"평가손익 {upnl:,.0f}" if upnl is not None else
    f"**보유**: {held_sym} · 수량 {qty} · 평단 {avg:,.0f}"
)
st.write(f"**다음 예상 행동**: {state.get('next_action') or '대기'}")
if state.get("order_block_reason"):
    st.warning(f"주문 보류: {state.get('order_block_reason')}")

# ── TP/SL / Continuation re-entry ─────────────────────────────────────────
st.subheader("TP/SL · 연속재진입")
ep = state.get("direction_episode") or {}
re = state.get("reentry") or {}
e1, e2, e3, e4 = st.columns(4)
e1.metric("마지막 이벤트", state.get("last_event") or "-")
e2.metric("에피소드 재진입 사용", "YES" if ep.get("continuation_reentry_used") else "NO")
e3.metric("SL 잠금", "YES" if ep.get("sl_lock") else "NO")
e4.metric(
    "재진입 기능",
    "ON" if state.get("continuation_reentry_enabled") else "OFF(기본)",
)
st.write(
    f"episode=`{ep.get('id')}` · dir=`{ep.get('direction')}` · "
    f"entry_kind=`{(pos.get('entry_kind') if pos else None) or '-'}` · "
    f"tp_at=`{ep.get('tp_at')}` · last_exit=`{ep.get('last_exit_reason')}`"
)
st.write(
    f"reentry_eligible=`{re.get('eligible')}` · block=`{re.get('block_reason')}` · "
    f"bars_since_tp=`{re.get('bars_since_tp')}` · hist_contracted=`{re.get('hist_contracted')}` · "
    f"hist_last3=`{re.get('hist_last3')}`"
)
st.caption(
    "Exit: TP +3% / SL -1.5% (net vs ETF entry) · 15:00 강제청산 최우선 · "
    "반대 MACD → OPPOSITE_SWITCH · CONTINUATION_REENTRY는 플래그 ON일 때만"
)

st.write(
    f"마지막 신호: `{state.get('last_signal_at')}` (`{state.get('last_signal_id')}`) · "
    f"마지막 주문: `{state.get('last_order_at')}`"
)

ww = state.get("worker") or {}
st.write(
    f"Worker alive=`{ww.get('alive') or wstatus.get('alive')}` · "
    f"last_tick=`{ww.get('last_tick_at') or wstatus.get('last_tick_at')}` · "
    f"intervals={ww.get('tick_intervals') or wstatus.get('tick_intervals')} · "
    f"avg={ww.get('avg_interval') or wstatus.get('avg_interval')} · "
    f"p95={ww.get('p95_interval') or wstatus.get('p95_interval')}"
)
st.write(
    f"signal_detected_at=`{ww.get('signal_detected_at')}` · "
    f"order_requested_at=`{ww.get('order_requested_at') or state.get('order_requested_at')}` · "
    f"broker_executed_at=`{ww.get('broker_executed_at') or state.get('broker_executed_at')}`"
)

# ── Pipeline ──────────────────────────────────────────────────────────────
st.subheader("실행 파이프라인")
pipe = state.get("pipeline") or {}
cols = st.columns(len(om.PIPELINE_STAGES))
for col, stage in zip(cols, om.PIPELINE_STAGES):
    info = pipe.get(stage) or {}
    ok = info.get("ok")
    mark = "✅" if ok is True else ("❌" if ok is False else "·")
    col.markdown(f"**{mark} {stage}**")
    col.caption(str(info.get("message") or "")[:60])
    if info.get("at"):
        col.caption(str(info.get("at"))[:19])

# ── Ledger ────────────────────────────────────────────────────────────────
st.subheader("MACD 전용 원장")
rows = om.load_ledger(limit=300)
if rows:
    df = pd.DataFrame(rows)
    show_cols = [
        c for c in [
            "timestamp", "macd_signal", "action", "symbol", "executed_qty",
            "order_price", "executed_price", "order_id", "hold_seconds",
            "gross_pnl", "cost", "net_pnl", "exit_reason", "entry_kind",
            "direction_episode_id", "signal_source", "mode", "git_sha",
            "success", "signal_id",
        ] if c in df.columns
    ]
    st.dataframe(df[show_cols].iloc[::-1], use_container_width=True, height=360)
else:
    st.info(f"원장 없음 — 경로: `{om.get_ledger_path()}`")

st.caption(f"state=`{om.STATE_PATH}` · mutex=`{om.MUTEX_PATH}` · sha=`{state.get('git_sha')}`")
