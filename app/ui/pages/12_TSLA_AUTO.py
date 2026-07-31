"""
12_TSLA_AUTO.py — ReadOnly UI for TSLA_AUTO (독립 신규 모듈)

TSLA_AUTO는 app/trading/tsla_auto/* 로 완전히 독립되어 있으며, 다른 자동매매
엔진 코드를 호출하지 않는다. UI는 command 기록(시작/중지/필터 토글)과
service.get_snapshot() 읽기만 수행한다 — MACD 계산·network 호출·Worker
생성/reload를 UI에서 직접 하지 않는다(docs/TSLA_AUTO_LOGIC.md §UI).

패널별로 격리되어 있어, 통계·원장 패널 하나가 오류가 나도 나머지 화면은
계속 렌더링된다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from datetime import datetime

import pandas as pd
import streamlit as st

from app.ui.auth_gate import require_login

require_login()

from app.config import get_config, get_kis_account_config, mask_account  # noqa: E402
from app.trading.tsla_auto import config as tsla_config  # noqa: E402
from app.trading.tsla_auto import ledger  # noqa: E402
from app.trading.tsla_auto import market_session  # noqa: E402
from app.trading.tsla_auto.models import RuntimeStatus  # noqa: E402
from app.trading.tsla_auto.service import get_service  # noqa: E402

ET = tsla_config.ET


def _format_dual_time(value) -> str:
    """{'et': iso, 'kst': iso} 딕셔너리 또는 iso 문자열을 'ET HH:MM:SS / KST HH:MM:SS'로."""
    if isinstance(value, dict):
        et_raw, kst_raw = value.get("et"), value.get("kst")
    else:
        et_raw, kst_raw = value, None
    if not et_raw:
        return "-"
    try:
        et_ts = pd.Timestamp(et_raw)
        et_str = et_ts.strftime("%H:%M:%S")
    except Exception:
        return str(et_raw)
    if kst_raw:
        try:
            kst_str = pd.Timestamp(kst_raw).strftime("%H:%M:%S")
            return f"ET {et_str} / KST {kst_str}"
        except Exception:
            pass
    return f"ET {et_str}"


def _worker_status(state, worker_stats: dict) -> str:
    if not state.auto_trade_on:
        return "STOPPED"
    if not worker_stats:
        return "STARTING"
    last_tick = worker_stats.get("last_tick_at")
    if not last_tick:
        return "STARTING"
    try:
        age = (datetime.now(ET) - datetime.fromisoformat(last_tick)).total_seconds()
    except Exception:
        return "STARTING"
    if age <= 10:
        return "RUNNING"
    if age <= tsla_config.WORKER_STALL_AGE_SEC:
        return "DELAYED"
    return "STALLED"


try:
    from streamlit_autorefresh import st_autorefresh

    st_autorefresh(interval=5000, key="tsla_auto_refresh")
except Exception:
    pass

st.set_page_config(page_title="TSLA_AUTO", layout="wide")
st.title("TSLA_AUTO")
st.caption(
    "완전 독립 신규 모듈 · MACD2/MACD v1/Enhanced와 상태·원장·Worker 미공유 · "
    "Read-only UI — command 기록과 snapshot 표시만 수행"
)

cfg = get_config()
service = get_service()
snapshot = service.get_snapshot()
state = snapshot["state"]

session_status = market_session.classify_session_status()
m0, m1, m2 = st.columns(3)
m0.metric("READ_ONLY/MOCK/REAL", state.mode)
m1.metric("전략 상태", state.ui_mode.value)
m2.metric("미국 세션", session_status)

# ── Controls (commands only) ────────────────────────────────────────────
st.subheader("계좌 / 제어")
c1, c2, c3, c4 = st.columns([1.2, 1.2, 1, 1])
with c1:
    mode = st.radio("모드", ["READ_ONLY", "MOCK", "REAL"], index=["READ_ONLY", "MOCK", "REAL"].index(state.mode) if state.mode in ("READ_ONLY", "MOCK", "REAL") else 0, horizontal=True, key="tsla_auto_mode")
with c2:
    budget_usd = st.number_input(
        "투자예산 (USD)", min_value=100.0, max_value=1_000_000.0,
        value=float(state.budget_usd or tsla_config.DEFAULT_BUDGET_USD), step=100.0, key="tsla_auto_budget",
    )
with c3:
    try:
        acct = get_kis_account_config("real" if mode == "REAL" else "mock")
        masked = acct.get("masked_account") or mask_account(acct.get("account_no", ""))
    except Exception:
        masked = None
    st.metric("계좌", masked or "(미설정)")
with c4:
    _worker_alive = bool(service.supervisor_status().get("worker_alive"))
    if state.auto_trade_on and _worker_alive:
        st.metric("주문 가능", "YES")
    elif state.auto_trade_on:
        st.metric("주문 가능", "STALLED")
    else:
        st.metric("주문 가능", "NO")

if mode == "REAL":
    st.error("REAL(실전) 모드 — 이번 배포에서는 지원하지 않는다(TSLA_AUTO_ALLOW_REAL_ORDER=false, KIS 해외 주문 TR 미확인).")

b1, b2, b3, b4 = st.columns(4)
with b1:
    if st.button("TSLA_AUTO 시작", type="primary", use_container_width=True):
        res = service.start(mode=mode, budget_usd=float(budget_usd))
        if res.get("ok"):
            st.rerun()
        else:
            st.error(f"시작 실패: {res.get('reason')}")
with b2:
    if st.button("TSLA_AUTO 중지", use_container_width=True):
        service.stop(liquidate=False)
        st.rerun()
with b3:
    if st.button("중지 및 일괄매도", use_container_width=True):
        service.stop(liquidate=True)
        st.rerun()
with b4:
    if st.button("Bootstrap 재시도", use_container_width=True):
        res = service.retry_bootstrap()
        st.caption(f"재시도 결과: ok={res.get('ok')} reason={res.get('reason')}")

# Optional Hybrid strong-flag filter toggle (command only — never places orders).
_filter_cols = st.columns([1.4, 1.6])
with _filter_cols[0]:
    _strong_on = st.checkbox(
        "강한 플래그만 거래", value=bool(getattr(state, "strong_filter_enabled", False)),
        key="tsla_auto_strong_filter_toggle",
        help="OFF=모든 LIVE_CONFIRMED 신호 주문권한 / ON=Hybrid 승인 신호만 주문권한",
    )
with _filter_cols[1]:
    if bool(_strong_on) != bool(getattr(state, "strong_filter_enabled", False)):
        res = service.set_strong_filter_enabled(bool(_strong_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"강한 플래그 필터 → {'ON' if _strong_on else 'OFF'} (다음 LIVE_CONFIRMED 신호부터 · `{res.get('strong_filter_enabled_at')}`)")
            st.rerun()
    else:
        st.caption(f"강한 플래그 필터={'ON' if state.strong_filter_enabled else 'OFF'} · version=`{getattr(state, 'strong_filter_version', None) or tsla_config.STRONG_FILTER_VERSION}`")

# ── State panel (isolated) ───────────────────────────────────────────────
st.subheader("상태 / 시세 / MACD")
try:
    worker_stats = snapshot.get("worker") or {}
    quotes = snapshot.get("quotes") or {}

    q1, q2, q3 = st.columns(3)
    for col, symbol, label in (
        (q1, tsla_config.SIGNAL_SYMBOL, "TSLA (신호 전용)"),
        (q2, tsla_config.LONG_SYMBOL, "TSLL (UP_RED)"),
        (q3, tsla_config.INVERSE_SYMBOL, "TSLZ (DOWN_BLUE)"),
    ):
        snap = quotes.get(symbol)
        with col:
            if snap is not None and getattr(snap, "price", 0):
                st.metric(label, f"${snap.price:,.2f}", help=f"age={snap.age_sec:.1f}s" if snap.age_sec is not None else None)
            else:
                st.metric(label, "-")

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Worker 상태", _worker_status(state, worker_stats))
    s2.metric("quote_status", snapshot.get("quote_status") or "-")
    s3.metric("당일 1분봉 수", state.today_1m_bar_count if state.today_1m_bar_count is not None else "-")
    s4.metric("market_regime", state.market_regime or "UNKNOWN")

    st.caption(f"block_reason: `{state.order_block_reason or '-'}` · worker_code_sha=`{snapshot.get('worker_code_sha') or '-'}`")

    ms1, ms2, ms3, ms4 = st.columns(4)
    ms1.metric("MACD", f"{snapshot.get('primary_macd'):.6f}" if snapshot.get("primary_macd") is not None else "-")
    ms2.metric("Signal", f"{snapshot.get('primary_signal'):.6f}" if snapshot.get("primary_signal") is not None else "-")
    ms3.metric("confirmed flag", state.latest_primary_flag.value if state.latest_primary_flag else "-")
    ms4.metric("마지막 완성 3분봉", state.last_completed_3m_bar_at or "-")

    st.markdown("**강한 플래그 (Hybrid) 판정**")
    fs1, fs2, fs3, fs4 = st.columns(4)
    fs1.metric("score", f"{state.last_score:.1f}" if state.last_score is not None else "-")
    fs2.metric("required", f"{state.last_required_score:.1f}" if state.last_required_score is not None else "-")
    fs3.metric("판정", state.last_decision or "-")
    fs4.metric("탈락 사유", state.last_block_reason or "-")
    if state.last_component_scores:
        st.dataframe(pd.DataFrame([state.last_component_scores]), use_container_width=True, height=80)

    st.markdown("**손절 재진입 쿨다운 (신규, docs §12)**")
    cd1, cd2, cd3 = st.columns(3)
    cd1.metric("쿨다운 방향", state.stop_loss_cooldown_direction.value if state.stop_loss_cooldown_direction else "-")
    cd2.metric("마지막 손절 시각", _format_dual_time(state.last_stop_loss_exit_at))
    cd3.metric("당일 예외 사용", "YES" if state.stop_loss_reentry_override_used_today else "NO")
except Exception as exc:
    st.error(f"상태 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")

# ── Position / order panel (isolated) ─────────────────────────────────────
st.subheader("포지션 / 주문")
try:
    pos = state.position
    p1, p2, p3 = st.columns(3)
    p1.metric("전략 보유 종목", pos.symbol if pos else "-")
    p2.metric("보유 수량", pos.quantity if pos else 0)
    p3.metric("평균단가", f"${pos.avg_price:,.2f}" if pos else "-")

    o1, o2, o3, o4 = st.columns(4)
    o1.metric("호가(ask1)", f"${state.last_order_ask1:,.2f}" if state.last_order_ask1 is not None else "-")
    o2.metric("주문가", f"${state.last_order_order_price:,.2f}" if state.last_order_order_price is not None else "-")
    o3.metric("final_qty", state.last_order_final_qty if state.last_order_final_qty is not None else "-")
    o4.metric("expected_notional_usd", f"${state.last_order_expected_notional_usd:,.2f}" if state.last_order_expected_notional_usd is not None else "-")

    r1, r2, r3 = st.columns(3)
    r1.metric("주문번호", state.last_broker_order_id or "-")
    r2.metric("체결수량", state.last_order_filled_qty if state.last_order_filled_qty is not None else "-")
    r3.metric("잔고수량", state.last_order_balance_qty if state.last_order_balance_qty is not None else "-")

    st.caption(
        f"KIS 응답: rt_cd=`{state.last_order_rt_cd or '-'}` · msg_cd=`{state.last_order_msg_cd or '-'}` · "
        f"msg1=`{state.last_order_msg1 or '-'}` · failure_stage=`{state.last_order_failure_stage or '-'}`"
    )
except Exception as exc:
    st.error(f"주문 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")

# ── Daily stats (isolated) ──────────────────────────────────────────────
st.subheader("오늘 신호·거래 통계")
try:
    trading_date = state.session_date or datetime.now(ET).strftime("%Y%m%d")
    current_sha = snapshot.get("worker_code_sha") or None
    sig_summary = ledger.summarize_signals(
        trading_date, strategy_version=state.strategy_version, signal_rule=state.signal_rule, worker_code_sha=current_sha,
    )
    trade_summary = ledger.summarize_daily_trading(trading_date, budget_usd=state.budget_usd)

    g1, g2, g3, g4 = st.columns(4)
    g1.metric("오늘 UP_RED", f"{sig_summary['red_count']}건")
    g2.metric("오늘 DOWN_BLUE", f"{sig_summary['blue_count']}건")
    g3.metric("완료 거래", f"{trade_summary['round_trip_count']}건")
    g4.metric("일일 진입 (신규진입/체결)", f"{state.daily_entry_count} / {tsla_config.NORMAL_MAX_ENTRIES if state.market_regime != 'CHOP' else tsla_config.CHOP_MAX_ENTRIES}")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Gross USD", f"${trade_summary['gross_pnl_usd']:,.2f}")
    m2.metric("비용 USD", f"${trade_summary['total_cost_usd']:,.2f}")
    m3.metric("Net USD", f"${trade_summary['net_pnl_usd']:,.2f}")
    m4.metric("수익률", f"{trade_summary['return_pct']:.4f}%")
    m5.metric("승률", f"{trade_summary['win_rate_pct']:.1f}%")

    # 오늘 전체 신호 개요 (재계산, 참고용) — LIVE_CONFIRMED vs HISTORICAL_REPLAY_ONLY
    overview = snapshot.get("today_signal_overview") or []
    _live = [r for r in overview if r.get("origin") == tsla_config.ORIGIN_LIVE_CONFIRMED]
    _hist = [r for r in overview if r.get("origin") == tsla_config.ORIGIN_HISTORICAL_REPLAY_ONLY]
    st.markdown("**오늘 전체 신호 개요 (재계산, 참고용 — 주문권한 없음)**")
    ov1, ov2 = st.columns(2)
    ov1.metric("LIVE_CONFIRMED (Worker 시작 이후)", f"{len(_live)}건")
    ov2.metric("HISTORICAL_REPLAY_ONLY (Worker 시작 전)", f"{len(_hist)}건")
    if overview:
        st.dataframe(pd.DataFrame(overview), use_container_width=True, height=180)

    excluded = sig_summary.get("excluded_signals") or []
    if excluded:
        with st.expander(f"과거/제외 신호 ({len(excluded)}건)"):
            st.dataframe(pd.DataFrame(excluded), use_container_width=True, height=220)
except Exception as exc:
    st.error(f"통계 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")

# ── Ledger (isolated) ───────────────────────────────────────────────────
st.subheader("거래 원장")
try:
    rows = ledger.load_execution_ledger(limit=300)
    trading_date = state.session_date or datetime.now(ET).strftime("%Y%m%d")
    today_rows = [r for r in rows if str(r.get("timestamp") or "").startswith(trading_date)]
    st.caption(f"execution ledger path=`{ledger.EXECUTION_LEDGER_PATH}` · loaded_rows={len(rows)} · today_rows={len(today_rows)}")
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df.iloc[::-1], use_container_width=True, height=300)
    else:
        st.caption("원장 없음")
except Exception as exc:
    st.error(f"원장 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")

# ── Session boundaries diagnostics (isolated) ────────────────────────────
st.subheader("미국시장 세션 진단")
try:
    today_et = datetime.now(ET).date()
    boundaries = market_session.session_boundaries(today_et)
    b1, b2, b3 = st.columns(3)
    b1.metric("정규장 개장", boundaries.market_open_et.strftime("%H:%M ET"))
    b2.metric("정규장 폐장", boundaries.market_close_et.strftime("%H:%M ET") + (" (조기폐장)" if boundaries.is_early_close else ""))
    b3.metric("조기폐장 여부", "YES" if boundaries.is_early_close else "NO")
    b4, b5, b6 = st.columns(3)
    b4.metric("신규진입 차단", boundaries.new_entry_cutoff_et.strftime("%H:%M ET"))
    b5.metric("강제청산 시작", boundaries.forced_liquidation_start_et.strftime("%H:%M ET"))
    b6.metric("최종 잔고확인", boundaries.final_balance_check_et.strftime("%H:%M ET"))
except Exception as exc:
    st.error(f"세션 진단 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")
