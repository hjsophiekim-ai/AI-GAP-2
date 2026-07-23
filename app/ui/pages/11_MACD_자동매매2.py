"""
11_MACD_자동매매2.py — ReadOnly UI for MACD2 (독립 신규 모듈)

MACD2는 app/trading/macd2/* 로 완전히 독립되어 있으며, 기존 MACD v1
(app/trading/macd_hynix_*, app/trading/macd_pipeline/*)이나 Enhanced 코드를
호출하지 않는다. UI는 command 기록(시작/중지)과 service.get_snapshot()
읽기만 수행한다 — MACD 계산·network 호출·Worker 생성/reload를 UI에서
직접 하지 않는다(docs/MACD2_LOGIC.md §16).

패널별로 격리되어 있어, 통계·원장 패널 하나가 오류가 나도 나머지 화면은
계속 렌더링된다(docs §16).
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
from app.trading.macd2 import config as macd2_config  # noqa: E402
from app.trading.macd2 import ledger  # noqa: E402
from app.trading.macd2.models import RuntimeStatus  # noqa: E402
from app.trading.macd2.service import get_service  # noqa: E402

try:
    from streamlit_autorefresh import st_autorefresh

    st_autorefresh(interval=5000, key="macd2_refresh")
except Exception:
    pass

st.set_page_config(page_title="MACD 자동매매2", layout="wide")
st.title("MACD 자동매매2")
st.caption(
    "완전 독립 신규 모듈 · MACD v1/Enhanced와 상태·원장 미공유 · "
    "Read-only UI — command 기록과 snapshot 표시만 수행"
)

cfg = get_config()
service = get_service()
snapshot = service.get_snapshot()
state = snapshot["state"]

st.metric("UI mode", state.ui_mode.value)

# ── Controls (commands only) ────────────────────────────────────────────
st.subheader("계좌 / 제어")
c1, c2, c3, c4 = st.columns([1.2, 1.2, 1, 1])
with c1:
    mode = st.radio("계좌 모드", ["mock", "real"], index=0 if state.mode != "real" else 1, horizontal=True, key="macd2_mode")
with c2:
    budget = st.number_input(
        "투자예산 (원)", min_value=100_000, max_value=500_000_000,
        value=int(state.budget or macd2_config.DEFAULT_BUDGET), step=100_000, key="macd2_budget",
    )
with c3:
    try:
        acct = get_kis_account_config(mode)
        masked = acct.get("masked_account") or mask_account(acct.get("account_no", ""))
    except Exception:
        masked = None
    st.metric("계좌", masked or "(미설정)")
with c4:
    # auto_trade_on is a persisted flag and can survive a Streamlit process
    # restart with no Worker actually re-started in this process — never show
    # order-ready from the flag alone (docs: Worker 부재+auto_trade_on=True는
    # STALLED로 표시).
    _worker_alive = bool(service.supervisor_status().get("worker_alive"))
    if state.auto_trade_on and _worker_alive:
        st.metric("주문 가능", "YES")
    elif state.auto_trade_on:
        st.metric("주문 가능", "STALLED")
    else:
        st.metric("주문 가능", "NO")

real_kwargs = {}
if mode == "real":
    st.error("REAL(실전) 모드 — 확인 문구 입력 후에만 시작 가능")
    expected = str(cfg.real_confirm_text() or "LIVE")
    confirm_in = st.text_input("REAL 확인 문구", type="password", key="macd2_real_confirm")
    real_toggle = st.checkbox("REAL 주문 활성화", key="macd2_real_toggle")
    real_kwargs = {
        "confirm_text": confirm_in, "runtime_real_mode": bool(real_toggle),
        "runtime_enable_real_buy": bool(real_toggle), "runtime_enable_real_sell": bool(real_toggle),
    }
else:
    st.info("MOCK 모드 (기본값) — KIS 모의투자 계좌")

b1, b2 = st.columns(2)
with b1:
    if st.button("자동매매 시작", type="primary", use_container_width=True):
        res = service.start(mode=mode, budget=float(budget), real_kwargs=real_kwargs if mode == "real" else None)
        if res.get("ok"):
            st.success("MACD2 자동매매 시작")
            st.rerun()
        else:
            st.error(res.get("message") or "시작 실패")
with b2:
    if st.button("자동매매 중지", use_container_width=True):
        service.stop("user_stop")
        st.warning("중지됨")
        st.rerun()

# Re-read after potential command
snapshot = service.get_snapshot()
state = snapshot["state"]
worker_stats = snapshot["worker"] or {}
quotes = snapshot["quotes"] or {}

# ── Status ────────────────────────────────────────────────────────────────
try:
    st.subheader("상태")
    s1, s2, s3 = st.columns(3)
    s1.metric("전략 상태", state.ui_mode.value)
    s2.metric("Worker 상태", "RUNNING" if worker_stats.get("tick_n", 0) and not worker_stats.get("stalled") else ("STALLED" if worker_stats.get("stalled") else "STOPPED"))
    s3.metric("bootstrap", "OK" if state.warmup_ready else "NOT_READY")

    st.write(f"block/error reason: `{state.order_block_reason or '-'}`")

    q1, q2, q3 = st.columns(3)
    for col, symbol, label in (
        (q1, macd2_config.WATCH_SYMBOL, "SK하이닉스 000660"),
        (q2, macd2_config.LONG_SYMBOL, "KODEX 0193T0"),
        (q3, macd2_config.INVERSE_SYMBOL, "SOL 0197X0"),
    ):
        snap = quotes.get(symbol)
        if snap is None:
            col.metric(label, "-")
        else:
            col.metric(label, f"{snap.price:,.0f}" if snap.price else "-", delta=f"age {snap.age_sec:.1f}s" if snap.age_sec is not None else None)

    p1, p2 = st.columns(2)
    if state.position:
        p1.metric("보유 종목", f"{state.position.symbol} · {state.position.quantity}주 · 평단 {state.position.avg_price:,.0f}")
    else:
        p1.metric("보유 종목", "flat")
    p2.metric("Profit Lock", "ON" if state.profit_lock_active else "OFF", delta=f"peak {state.peak_net_return:.2f}%")

    st.caption(f"최신 signal_id: `{(state.processed_signal_ids or ['-'])[-1]}` · last_signal_direction: `{state.last_signal_direction.value if state.last_signal_direction else '-'}`")
except Exception as exc:
    st.error(f"상태 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")

# ── Daily stats (isolated) ──────────────────────────────────────────────
st.subheader("오늘 신호·거래 통계")
try:
    trading_date = (state.session_date or pd.Timestamp.now().strftime("%Y%m%d"))
    sig_summary = ledger.summarize_signals(trading_date)
    trade_summary = ledger.summarize_daily_trading(trading_date, budget=state.budget)

    g1, g2, g3 = st.columns(3)
    g1.metric("오늘 빨간 플래그", f"{sig_summary['red_count']}회")
    g2.metric("오늘 파란 플래그", f"{sig_summary['blue_count']}회")
    g3.metric("완료 왕복", f"{trade_summary['round_trip_count']}건")

    for u in sig_summary.get("unexecuted_signals") or []:
        st.write(f"- `{u.get('signal_id')}` · {u.get('direction')} · 사유 `{u.get('reason')}`")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Gross", f"{trade_summary['gross_pnl']:,.0f}원")
    m2.metric("비용", f"{trade_summary['total_cost']:,.0f}원")
    m3.metric("Net", f"{trade_summary['net_pnl']:,.0f}원")
    m4.metric("수익률", f"{trade_summary['return_pct']:.4f}%")
    m5.metric("승률", f"{trade_summary['win_rate_pct']:.1f}%")
except Exception as exc:
    st.error(f"통계 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")

# ── Ledger (isolated) ───────────────────────────────────────────────────
st.subheader("거래 원장")
try:
    rows = ledger.load_execution_ledger(limit=300)
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df.iloc[::-1], use_container_width=True, height=360)
    else:
        st.caption("원장 없음")
except Exception as exc:
    st.error(f"원장 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")
