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
from app.trading.macd_hynix_ledger import summarize_daily_trading, summarize_order_latency  # noqa: E402
from app.trading.macd_hynix_strategy import (  # noqa: E402
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SIGNAL_SYMBOL,
)
from app.utils.time_utils import kst_now  # noqa: E402

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
    expected = str(cfg.real_confirm_text() or "LIVE")
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

# ── Daily trading summary ─────────────────────────────────────────────────
st.subheader("오늘 거래 요약")
_budget_for_pnl = float(state.get("budget") or budget or 10_000_000)
_daily = summarize_daily_trading(budget=_budget_for_pnl)
_today_label = kst_now().strftime("%Y-%m-%d")
if _daily.get("has_data"):
    p1, p2, p3, p4, p5 = st.columns(5)
    p1.metric("오늘 총 거래 건수", f"{_daily.get('round_trip_count', 0):,}건")
    p2.metric("수수료", f"{_daily.get('total_cost', 0):,.0f}원")
    p3.metric("손실", f"{_daily.get('loss_amount', 0):,.0f}원")
    p4.metric("수익", f"{_daily.get('profit_amount', 0):,.0f}원")
    _net = float(_daily.get("net_pnl") or 0)
    p5.metric(
        "수익률",
        f"{_daily.get('return_pct', 0):.4f}%",
        delta=f"순손익 {_net:+,.0f}원",
    )
    st.caption(
        f"KST 거래일 `{_today_label}` · 원장 `{_daily.get('ledger_path')}` · "
        f"완료 왕복거래(매도 체결) {_daily.get('round_trip_count', 0)}건 · "
        f"체결 {_daily.get('operating_fill_count', 0)}건 "
        f"(매수 {_daily.get('buy_fill_count', 0)} / 매도 {_daily.get('sell_fill_count', 0)}) · "
        f"수익률 = 순손익 / 투자예산({int(_budget_for_pnl):,}원) × 100"
    )
else:
    z1, z2, z3, z4, z5 = st.columns(5)
    z1.metric("오늘 총 거래 건수", "0건")
    z2.metric("수수료", "0원")
    z3.metric("손실", "0원")
    z4.metric("수익", "0원")
    z5.metric("수익률", "0.0000%")
    st.caption(
        f"KST 거래일 `{_today_label}` · 데이터 없음 — 원장 `{_daily.get('ledger_path')}` · "
        f"수익률 기준 = 투자예산({int(_budget_for_pnl):,}원, state.budget)"
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

# ── Opening probe ─────────────────────────────────────────────────────────
st.subheader("Opening probe (09:00 immediate)")
op = state.get("opening_probe") or {}
probe_on = bool(state.get("opening_probe_enabled"))
p1, p2, p3, p4 = st.columns(4)
p1.metric("Probe live flag", "ON" if probe_on else "OFF(기본)")
p2.metric("Warm-up ready", "YES" if op.get("warmup_ready") else "NO")
p3.metric("09:00 fired", "YES" if op.get("immediate_fired_today") else "NO")
p4.metric("Await 09:03", "YES" if op.get("awaiting_09_03_confirm") else "NO")
st.write(
    f"warmup_reason=`{op.get('warmup_reason')}` · hist_last2=`{op.get('warmup_hist_last2')}` · "
    f"deltas=`{op.get('warmup_hist_deltas')}` · day_open=`{op.get('day_open_price')}` · "
    f"window_active=`{op.get('window_active')}` · abandoned=`{op.get('window_abandoned')}` · "
    f"last_eval=`{op.get('last_eval_signal')}`/`{op.get('last_eval_reason')}` · "
    f"scaled=`{op.get('scaled_to_full')}` · unconf_exit=`{op.get('unconfirmed_exit_at')}`"
)
st.caption(
    "09:00:05–09:00:15 immediate 50% (warm-up MACD + price/slope) · "
    "09:03 first 3m bar confirm → scale 100% or flatten · "
    f"OPENING_PROBE_ENABLED={probe_on} (replay ADOPT gates apply)"
)

# ── TP/SL / Continuation re-entry ─────────────────────────────────────────
st.subheader("Profit lock · SL · 연속재진입")
ep = state.get("direction_episode") or {}
re = state.get("reentry") or {}
pl = state.get("profit_lock") or {}
e1, e2, e3, e4 = st.columns(4)
e1.metric("마지막 이벤트", state.get("last_event") or "-")
e2.metric("에피소드 재진입 사용", "YES" if ep.get("continuation_reentry_used") else "NO")
e3.metric("SL 잠금", "YES" if ep.get("sl_lock") else "NO")
e4.metric(
    "재진입 기능",
    "ON" if state.get("continuation_reentry_enabled") else "OFF(기본)",
)
p1, p2, p3, p4 = st.columns(4)
p1.metric("Lock active", "YES" if pl.get("profit_lock_active") else "NO")
p2.metric("Peak net %", f"{float(pl.get('peak_net_return') or 0):.3f}")
p3.metric("Current net %", f"{float(pl.get('current_net_return') or 0):.3f}")
p4.metric("Giveback pp", f"{float(pl.get('giveback_pct') or 0):.3f}")
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
    "Exit: PROFIT_LOCK (activate ≥+1.5% net, giveback ≥0.8pp from peak) / SL −1.5% · "
    "15:00 강제청산 최우선 · 반대 MACD → OPPOSITE_SWITCH · 고정 +3% TP 없음 · "
    "CONTINUATION_REENTRY는 플래그 ON일 때만"
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
    f"kis_order_accepted_at=`{ww.get('kis_order_accepted_at') or state.get('kis_order_accepted_at')}` · "
    f"broker_executed_at=`{ww.get('broker_executed_at') or state.get('broker_executed_at')}` · "
    f"position_confirmed_at=`{ww.get('position_confirmed_at') or state.get('position_confirmed_at')}`"
)

# ── Order latency instrumentation ─────────────────────────────────────────
st.subheader("주문 지연 계측")
lat = summarize_order_latency(
    state=state,
    tick_intervals=list(ww.get("tick_intervals") or wstatus.get("tick_intervals") or []),
    main_cycle_3m_wait_count=int(
        ww.get("main_cycle_3m_wait_count")
        if ww.get("main_cycle_3m_wait_count") is not None
        else (wstatus.get("main_cycle_3m_wait_count") or 0)
    ),
)
verdict = lat.get("verdict") or "NOT_MEASURED"
st.metric("Latency verdict", verdict)
st.caption(
    f"samples n=`{lat.get('sample_count')}` · "
    f"3m-cycle waits=`{lat.get('main_cycle_3m_wait_count')}` "
    f"(MACD는 5초 worker 전용 — Enhanced 3분 메인사이클 대기 없음) · "
    f"ledger=`{lat.get('ledger_path')}`"
)

_seg_labels = {
    "bar_complete_to_signal_detect": "1) 3m bar complete → signal detect",
    "signal_detect_to_order_request": "2) signal detect → order request",
    "order_request_to_kis_accept": "3) order request → KIS accept",
    "kis_accept_to_fill_confirm": "4) KIS accept → fill confirm",
    "signal_detect_to_final_fill": "5) signal detect → final fill (E2E)",
}
seg_rows = []
for key, label in _seg_labels.items():
    s = (lat.get("segments") or {}).get(key) or {}
    seg_rows.append({
        "segment": label,
        "n": s.get("n") or 0,
        "median_s": s.get("median"),
        "p95_s": s.get("p95"),
        "max_s": s.get("maximum"),
        "over_10s": s.get("over_10s_count") or 0,
    })
# Extra: signal→KIS (gate)
sk = lat.get("signal_detect_to_kis_accept") or {}
seg_rows.append({
    "segment": "signal detect → KIS accept (gate)",
    "n": sk.get("n") or 0,
    "median_s": sk.get("median"),
    "p95_s": sk.get("p95"),
    "max_s": sk.get("maximum"),
    "over_10s": sk.get("over_10s_count") or 0,
})
st.dataframe(pd.DataFrame(seg_rows), use_container_width=True, hide_index=True)

tick = lat.get("worker_tick") or {}
t1, t2, t3, t4 = st.columns(4)
t1.metric("Worker tick n", tick.get("n") or 0)
t2.metric("Worker tick mean (≤5.5s)", tick.get("mean"))
t3.metric("Worker tick p95 (≤7s)", tick.get("p95"))
t4.metric("Worker tick max", tick.get("maximum"))
if verdict == "NOT_MEASURED":
    st.info("실신호 latency 샘플이 없어 게이트 판정은 NOT_MEASURED 입니다. 계측 코드는 배포됨.")

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
            "gross_pnl", "cost", "net_pnl", "exit_reason",
            "peak_net_return", "current_net_return", "giveback_pct", "profit_lock_active",
            "entry_kind", "direction_episode_id", "signal_source", "mode", "git_sha",
            "success", "signal_id",
            "completed_3m_bar_at", "signal_detected_at", "order_requested_at",
            "kis_order_accepted_at", "broker_executed_at", "position_confirmed_at",
            "lat_bar_to_signal_s", "lat_signal_to_request_s", "lat_request_to_kis_s",
            "lat_kis_to_fill_s", "lat_signal_to_fill_s",
        ] if c in df.columns
    ]
    st.dataframe(df[show_cols].iloc[::-1], use_container_width=True, height=360)
else:
    st.info(f"원장 없음 — 경로: `{om.get_ledger_path()}`")

st.caption(f"state=`{om.STATE_PATH}` · mutex=`{om.MUTEX_PATH}` · sha=`{state.get('git_sha')}`")
