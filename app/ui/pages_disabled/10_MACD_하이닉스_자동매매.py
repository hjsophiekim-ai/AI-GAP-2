"""
10_MACD_하이닉스_자동매매.py — ReadOnly UI

Reads one runtime snapshot. Writes ONLY start/stop/force_liquidate commands.
Does NOT create/kill/reload Worker, compute MACD, or gate orders via session_state.
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
from app.trading.macd_hynix_ledger import summarize_daily_trading, summarize_order_latency  # noqa: E402
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
    "Read-only UI · Worker 단일 루프가 신호→주문 소유 · "
    "Strategy B signed-B · ETF 주문 전용 · Enhanced 분리"
)

cfg = get_config()
# READ ONLY — do not call ensure_worker_running() for lifecycle ownership.
state = om.load_state()
wstatus = worker.get_worker_status()
_stall = worker.detect_worker_stall(state=state, status=wstatus)
ui_mode = str(state.get("ui_mode") or ("RUNNING" if state.get("auto_trade_on") else "STOPPED"))

st.metric("UI mode", ui_mode)
if wstatus.get("stale_worker"):
    st.warning(
        f"코드 SHA 불일치 (reload 금지 — 프로세스/Worker 재시작 필요). "
        f"loaded=`{wstatus.get('loaded_git_sha')}` disk=`{wstatus.get('disk_git_sha')}`"
    )
if _stall.get("stalled") or ui_mode == "WORKER_STALLED":
    st.error(f"WORKER_STALLED age=`{_stall.get('tick_age_sec')}`s — Stop→Start로 새 Worker 객체 기동")

# ── Controls (commands only) ──────────────────────────────────────────────
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
    expected = str(cfg.real_confirm_text() or "LIVE")
    confirm_in = st.text_input("REAL 확인 문구", type="password", key="macd_real_confirm")
    real_toggle = st.checkbox("REAL 주문 활성화", key="macd_real_toggle")
    real_confirm_ok = bool(real_toggle and confirm_in == expected)
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
            st.success("MACD 자동매매 시작 (새 Worker 객체)")
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
        st.warning("전량청산 요청 — Worker 루프가 처리")
        st.rerun()

# Re-read after potential command
state = om.load_state()
wstatus = worker.get_worker_status()

old_on, old_src = om.read_old_auto_trade_on()
if old_on:
    st.error(f"Enhanced 자동매매 ON (`{old_src}`) — MACD 시작 불가. Enhanced를 먼저 중지하세요.")

# Daily PnL
_today_label = (state.get("session_date") or "")[:10]
_budget_for_pnl = float(state.get("budget") or 10_000_000)
_daily = summarize_daily_trading(state=state)
st.subheader("오늘 손익")
z1, z2, z3, z4, z5 = st.columns(5)
z1.metric("실현 손익", f"{int(_daily.get('realized_net') or 0):,}원")
z2.metric("평가 손익", f"{int(_daily.get('unrealized_net') or 0):,}원")
z3.metric("손실", f"{int(_daily.get('loss_sum') or 0):,}원")
z4.metric("수익", f"{int(_daily.get('profit_sum') or 0):,}원")
z5.metric("수익률", f"{float(_daily.get('return_pct') or 0):.4f}%")

# Flag summary
st.subheader("오늘 신호·미주문 요약")
_flag_sum = om.summarize_macd_flag_events(state)
_trade_n = int(_daily.get("round_trip_count") or 0)
fg1, fg2, fg3 = st.columns(3)
fg1.metric("오늘 빨간불", f"{_flag_sum.get('red_count', 0)}회")
fg2.metric("오늘 파란불", f"{_flag_sum.get('blue_count', 0)}회")
fg3.metric("오늘 거래", f"{_trade_n}건")
_missed = _flag_sum.get("missed_order_events") or []
_warmup_no = not (
    state.get("warmup_ready")
    or (state.get("opening_probe") or {}).get("warmup_ready")
    or (state.get("bootstrap") or {}).get("ok")
)
if _missed:
    for _m in _missed:
        _fl = _m.get("flag")
        _label = "빨간불(UP_RED)" if _fl == DIR_UP else ("파란불(DOWN_BLUE)" if _fl == DIR_DOWN else str(_fl))
        st.write(f"- `{_m.get('ts')}` · {_label} · `{_m.get('signal_id')}` · `{_m.get('block_reason')}`")
elif _warmup_no or str(state.get("macd_status") or "") == "NOT_READY":
    st.warning("신호 계산 불가 — warmup_ready=NO / NOT_READY")
else:
    st.caption("미주문 신호 없음 (모든 당일 신호 주문 완료 또는 신호 없음)")

# Status split
st.subheader("상태 분리")
s1, s2, s3, s4, s5, s6 = st.columns(6)
s1.metric("scheduler_alive", "YES" if (wstatus.get("alive") or (state.get("worker") or {}).get("alive")) else "NO")
s2.metric("strategy_enabled", "YES" if state.get("auto_trade_on") else "NO")
s3.metric("market_data_active", "YES" if state.get("market_data_active") else "NO")
s4.metric("signal_calculation_active", "YES" if state.get("signal_calculation_active") else "NO")
s5.metric("order_execution_enabled", "YES" if state.get("order_execution_enabled") else "NO")
s6.metric("primary_block_reason", str(state.get("primary_block_reason") or "-")[:40])

_boot = state.get("bootstrap") or {}
_op = state.get("opening_probe") or {}
st.subheader("Bootstrap / Warm-up")
b1, b2, b3, b4, b5 = st.columns(5)
b1.metric("bootstrap", str(_boot.get("status") or "-"))
b2.metric("warmup_ready", "YES" if state.get("warmup_ready") or _op.get("warmup_ready") or _boot.get("ok") else "NO")
b3.metric("1m bars", str(_boot.get("received_1m_bars") or "-"))
b4.metric("3m count", str(_boot.get("completed_3m_count") or "-"))
b5.metric("prior_1m", str(_boot.get("prior_day_1m_bars") or "-"))

# Diagnostics from completed_signal_snapshot ONLY
st.subheader("현재 진단")
_cs = state.get("completed_signal_snapshot") or state.get("completed_signal") or {}
_not_ready = (
    str(state.get("macd_status") or "") == "NOT_READY"
    or str(_cs.get("flag") or "") == "NOT_READY"
    or _warmup_no
)
light = _cs.get("flag") or DIR_HOLD
if _not_ready:
    light_label = "⛔ NOT_READY (신호 계산 불가)"
elif light == DIR_UP:
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
if _not_ready:
    d1.write("**MACD**: NOT_READY")
    d2.write("**Signal**: NOT_READY")
    d3.write("**Histogram**: NOT_READY")
else:
    d1.write(f"**MACD**: {macd.get('macd') if macd.get('macd') is not None else _cs.get('macd')}")
    d2.write(f"**Signal**: {macd.get('signal') if macd.get('signal') is not None else _cs.get('signal')}")
    d3.write(f"**Histogram**: {macd.get('hist') if macd.get('hist') is not None else _cs.get('hist')}")
    st.write(f"hist3=`{_cs.get('hist_last3') or macd.get('hist_last3')}` · reason=`{_cs.get('reason')}`")

if state.get("quote_errors"):
    st.error("시세 조회 오류:")
    for err in state["quote_errors"]:
        st.code(
            f"api={err.get('api_function')} symbol={err.get('symbol')} "
            f"msg={err.get('error_message')} rt_cd={err.get('rt_cd')} elapsed={err.get('elapsed_sec')}"
        )

st.write(
    f"**보유**: {pos.get('symbol') or '-'} · 수량 {int(pos.get('quantity') or 0)} · "
    f"평단 {float(pos.get('avg_price') or 0):,.0f}"
)
if state.get("order_block_reason"):
    st.warning(f"주문 보류: {state.get('order_block_reason')}")

st.subheader("신호·주문 라이프사이클")
sf1, sf2, sf3, sf4 = st.columns(4)
sf1.metric("current_flag", str(_cs.get("flag") or "-"))
sf2.metric("lifecycle", str(state.get("signal_lifecycle") or "-"))
sf3.metric("signal_id", str(_cs.get("signal_id") or state.get("last_signal_id") or "-")[:36])
sf4.metric("quote_age", str(state.get("quote_cache_age_sec") or (state.get("quotes") or {}).get("age_sec") or "-"))

ww = state.get("worker") or {}
st.write(
    f"Worker alive=`{ww.get('alive') or wstatus.get('alive')}` · "
    f"instance=`{wstatus.get('worker_instance_id') or state.get('worker_instance_id')}` · "
    f"started=`{wstatus.get('worker_started_at') or state.get('worker_started_at')}` · "
    f"threads=`{wstatus.get('worker_thread_count')}` · "
    f"quote_updater=`{wstatus.get('quote_updater_alive')}` · "
    f"tick_seq=`{ww.get('tick_seq') or wstatus.get('tick_seq')}` · "
    f"avg=`{ww.get('avg_interval') or wstatus.get('avg_interval')}` · "
    f"p95=`{ww.get('p95_interval') or wstatus.get('p95_interval')}` · "
    f"sha=`{state.get('worker_code_sha') or wstatus.get('worker_code_sha')}`"
)
if wstatus.get("last_exception"):
    st.code(str(wstatus.get("last_exception_traceback") or wstatus.get("last_exception"))[:2000])

st.subheader("주문 지연 계측")
lat = summarize_order_latency(
    state=state,
    tick_intervals=list(ww.get("tick_intervals") or wstatus.get("tick_intervals") or []),
    main_cycle_3m_wait_count=0,
)
st.metric("Latency verdict", lat.get("verdict") or "NOT_MEASURED")
st.caption(f"samples n=`{lat.get('sample_count')}` · goals: signal→intent≤1s, intent→KIS≤4s")

st.subheader("실행 파이프라인")
pipe = state.get("pipeline") or {}
cols = st.columns(len(om.PIPELINE_STAGES))
for col, stage in zip(cols, om.PIPELINE_STAGES):
    info = pipe.get(stage) or {}
    ok = info.get("ok")
    mark = "✅" if ok is True else ("❌" if ok is False else "·")
    col.markdown(f"**{mark} {stage}**")
    col.caption(str(info.get("message") or "")[:60])

st.subheader("MACD 전용 원장")
rows = om.load_ledger(limit=300)
if rows:
    df = pd.DataFrame(rows)
    show_cols = [c for c in df.columns if c in (
        "timestamp", "macd_signal", "action", "symbol", "executed_qty",
        "order_price", "executed_price", "net_pnl", "exit_reason", "signal_id",
        "success", "lat_signal_to_request_s", "lat_request_to_kis_s",
    )]
    st.dataframe(df[show_cols].iloc[::-1], use_container_width=True, height=360)
else:
    st.caption("원장 없음")
