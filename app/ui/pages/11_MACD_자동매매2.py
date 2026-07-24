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

from datetime import datetime, time as dtime

import pandas as pd
import streamlit as st

from app.ui.auth_gate import require_login

require_login()

from app.config import get_config, get_kis_account_config, mask_account  # noqa: E402
from app.trading.macd2 import config as macd2_config  # noqa: E402
from app.trading.macd2 import ledger  # noqa: E402
from app.trading.macd2.models import RuntimeStatus  # noqa: E402
from app.trading.macd2.service import get_service  # noqa: E402
from app.utils.runtime_info import read_runtime_info  # noqa: E402

# Display-only threshold, not an order-blocking rule (that remains
# macd2_config.FORCE_LIQUIDATE_AT/NEW_ENTRY_CUTOFF, untouched) — used only to
# tell "장 마감 후 대기" apart from "장전 대기" when bootstrap has no today
# bars yet (docs §21 2026-07-24 bootstrap-diagnostics UI addition).
_MARKET_CLOSE_HINT = dtime(15, 30)


def _worker_status(state, worker_stats: dict) -> str:
    """STOPPED (정상, auto_trade_on=False) / STARTING (스레드 있음, 아직 tick
    없음) / RUNNING (<=10s) / DELAYED (10~15s) / STALLED (>15s) / DEAD
    (auto_trade_on=True인데 Worker 스레드/객체 자체가 없음 — 프로세스 재시작
    후 복구되지 않은 상태)."""
    if not state.auto_trade_on:
        return "STOPPED"
    if not worker_stats:
        return "DEAD"
    age = worker_stats.get("last_tick_age_sec")
    if age is None:
        return "STARTING"
    if age <= 10:
        return "RUNNING"
    if age <= 15:
        return "DELAYED"
    return "STALLED"


def _quote_status(quotes: dict) -> str:
    for symbol in (macd2_config.WATCH_SYMBOL, macd2_config.LONG_SYMBOL, macd2_config.INVERSE_SYMBOL):
        snap = quotes.get(symbol)
        if snap is None or snap.error or not snap.price:
            return "PARTIAL_ERROR"
        if snap.age_sec is not None and snap.age_sec > macd2_config.QUOTE_MAX_AGE_SEC:
            return "PARTIAL_STALE"
    return "READY"


def _bootstrap_status(state, bootstrap_last_result: dict | None) -> str:
    if state.warmup_ready:
        return "OK"
    reason = str((bootstrap_last_result or {}).get("reason") or state.order_block_reason or "")
    if "NO_1M_BARS" in reason or "TODAY_ONLY_WARMING_UP" in reason:
        now_t = datetime.now(macd2_config.KST).time()
        if now_t < macd2_config.SESSION_OPEN:
            return "PREMARKET_WAIT"
        if now_t >= _MARKET_CLOSE_HINT:
            return "MARKET_CLOSED_WAIT"
    return "FAILED" if bootstrap_last_result is not None or state.order_block_reason else "PENDING"

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

b1, b2, b3 = st.columns(3)
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
with b3:
    if st.button("Bootstrap 재시도", use_container_width=True):
        res = service.retry_bootstrap()
        if res.get("ok"):
            st.success(res.get("message") or "Bootstrap 재시도 성공")
        else:
            st.error(res.get("message") or "Bootstrap 재시도 실패")
        st.rerun()

# Re-read after potential command
snapshot = service.get_snapshot()
state = snapshot["state"]
worker_stats = snapshot["worker"] or {}
quotes = snapshot["quotes"] or {}

# ── Status ────────────────────────────────────────────────────────────────
try:
    st.subheader("상태")
    bootstrap_last_result = snapshot.get("bootstrap_last_result")
    quote_status = snapshot.get("quote_status") or _quote_status(quotes)
    bootstrap_status = _bootstrap_status(state, bootstrap_last_result)
    macd_status = "READY" if state.warmup_ready else "NOT_READY"
    order_status = "BLOCKED" if state.order_block_reason or not state.auto_trade_on else "READY"

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("전략 상태", state.ui_mode.value)
    s2.metric("Worker 상태", _worker_status(state, worker_stats))
    s3.metric("quote_status", quote_status)
    s4.metric("bootstrap_status", bootstrap_status)

    st.caption(f"macd_status=`{macd_status}` · order_status=`{order_status}`")
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

# ── 운영 진단 (Worker heartbeat + bootstrap/quote diagnostics, isolated) ──
st.subheader("운영 진단")
try:
    sup = service.supervisor_status()
    bootstrap_diag = snapshot.get("bootstrap_diag") or {}
    kis_pages = bootstrap_diag.get("kis_pages") or []
    prior_day_cache = bootstrap_diag.get("prior_day_cache") or {}
    worker_code_sha = (read_runtime_info() or {}).get("git_sha")

    d1, d2, d3 = st.columns(3)
    d1.metric("worker_status", _worker_status(state, worker_stats))
    d2.metric("quote_updater_status", "READY" if sup.get("quote_updater_alive") else "STOPPED")
    d3.metric("active_worker_count", sup.get("active_worker_count", 0))

    st.markdown("**Worker**")
    w1, w2, w3, w4 = st.columns(4)
    w1.metric("worker_instance_id", worker_stats.get("instance_id") or "-")
    w2.metric("worker_started_at", worker_stats.get("started_at") or "-")
    w3.metric("worker_code_sha", (worker_code_sha or "-")[:12])
    w4.metric("tick_seq_total", worker_stats.get("tick_n", 0))

    w5, w6, w7, w8 = st.columns(4)
    w5.metric("recent_tick_sample_count", worker_stats.get("recent_tick_sample_count", 0))
    w6.metric("last_tick_at", worker_stats.get("last_tick_at") or "-")
    last_tick_age = worker_stats.get("last_tick_age_sec")
    w7.metric("last_tick_age_sec", f"{last_tick_age:.1f}" if last_tick_age is not None else "-")
    w8.metric("next_tick_at", worker_stats.get("next_tick_at") or "-")

    w9, w10, w11 = st.columns(3)
    mean_iv, p95_iv, max_iv = (
        worker_stats.get("mean_interval_sec"), worker_stats.get("p95_interval_sec"), worker_stats.get("max_interval_sec"),
    )
    w9.metric("tick mean(s)", f"{mean_iv:.2f}" if mean_iv is not None else "-")
    w10.metric("tick p95(s)", f"{p95_iv:.2f}" if p95_iv is not None else "-")
    w11.metric("tick max(s)", f"{max_iv:.2f}" if max_iv is not None else "-")
    if worker_stats.get("last_exception"):
        st.error(f"Worker last_exception: `{worker_stats['last_exception']}`")

    st.markdown("**Quote**")
    for symbol, label in (
        (macd2_config.WATCH_SYMBOL, "SK하이닉스 000660"),
        (macd2_config.LONG_SYMBOL, "KODEX 0193T0"),
        (macd2_config.INVERSE_SYMBOL, "SOL 0197X0"),
    ):
        snap = quotes.get(symbol)
        if snap is None:
            st.write(f"- `{symbol}` ({label}): price=- fetched_at=- age=- error=-")
        else:
            fetched_at = snap.fetched_at.isoformat() if snap.fetched_at else "-"
            age = f"{snap.age_sec:.1f}s" if snap.age_sec is not None else "-"
            st.write(
                f"- `{symbol}` ({label}): price={snap.price:,.0f} fetched_at={fetched_at} "
                f"age={age} error=`{snap.error or '-'}`"
            )

    st.markdown("**Bootstrap**")
    bs1, bs2, bs3 = st.columns(3)
    bs1.metric("bootstrap_last_attempt_at", snapshot.get("bootstrap_last_attempt_at") or "-")
    bs2.metric("bootstrap_retry_count", snapshot.get("bootstrap_attempts", 0))
    bs3.metric("requested trading date", bootstrap_diag.get("requested_trading_date") or "-")

    bs4, bs5, bs6 = st.columns(3)
    bs4.metric("received_1m_bars", (bootstrap_last_result or {}).get("received_1m_bars", "-"))
    bs5.metric("completed_3m_bars", (bootstrap_last_result or {}).get("completed_3m_count", "-"))
    bs6.metric("warmup_ready", "YES" if state.warmup_ready else "NO")

    st.caption(
        f"merged oldest/newest: `{bootstrap_diag.get('merged_oldest') or '-'}` ~ "
        f"`{bootstrap_diag.get('merged_newest') or '-'}` · "
        f"prior_day_cache: date=`{prior_day_cache.get('prior_trading_date') or '-'}` "
        f"count={prior_day_cache.get('received_count', '-')} error=`{prior_day_cache.get('error') or '-'}`"
    )

    if kis_pages:
        with st.expander(f"KIS 분봉 요청 상세 ({len(kis_pages)}건)"):
            st.dataframe(pd.DataFrame(kis_pages), use_container_width=True)

    st.caption(f"정확한 block_reason: `{state.order_block_reason or '-'}`")
except Exception as exc:
    st.error(f"운영 진단 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")

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
