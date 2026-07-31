"""
11_MACD_자동매매2.py — ReadOnly UI for MACD2 (독립 신규 모듈)

MACD2는 app/trading/macd2/* 로 완전히 독립되어 있으며, 다른 자동매매
엔진 코드를 호출하지 않는다. UI는 command 기록(시작/중지)과 service.get_snapshot()
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


def _broker_order_result_display(state) -> str:
    result = str(state.last_broker_order_result or "").strip()
    if not result:
        return "-"
    legacy_cash_reject = "주문가능금액" in result and state.last_order_nrcvb_buy_qty is None and state.last_order_final_qty is None
    if legacy_cash_reject:
        return "LEGACY_ORDER_REJECTED_PRE_FIX"
    return result


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


def _format_signal_time(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    try:
        parsed = pd.Timestamp(text)
        if not pd.isna(parsed):
            return parsed.strftime("%H:%M:%S")
    except Exception:
        pass
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 14:
        return f"{digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
    if len(digits) >= 6:
        return f"{digits[-6:-4]}:{digits[-4:-2]}:{digits[-2:]}"
    return text


def _signal_display_time(row: dict) -> str:
    return _format_signal_time(
        row.get("forming_bar_start")
        or row.get("signal_bar_at")
        or row.get("completed_bar_at")
    )


def _order_requested_at(row: dict) -> str:
    return _format_signal_time(row.get("order_requested_at") or row.get("order_requested_at_trace"))


def _not_ordered_reason(row: dict, requested_at: str) -> str:
    if requested_at != "-":
        return "-"
    for key in ("block_reason", "final_result", "order_result"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "NO_ORDER_REQUEST"


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

# Optional Hybrid MAJOR_FLAG filter toggle (command only — never places orders).
_filter_cols = st.columns([1.4, 1.6])
with _filter_cols[0]:
    _major_on = st.checkbox(
        "강한 플래그만 거래",
        value=bool(getattr(state, "major_filter_enabled", False)),
        key="macd2_major_filter_toggle",
        help="OFF=기존 confirmed 신호 전부 / ON=MAJOR_FLAG 승인 신호만 주문권한",
    )
with _filter_cols[1]:
    if bool(_major_on) != bool(getattr(state, "major_filter_enabled", False)):
        res = service.set_major_filter_enabled(bool(_major_on), changed_by="ui")
        if res.get("ok"):
            st.caption(
                f"Major filter → {'ON' if _major_on else 'OFF'} "
                f"(다음 confirmed 플래그부터 · `{res.get('major_filter_enabled_at')}`)"
            )
            st.rerun()
    else:
        st.caption(
            f"Major filter={'ON' if state.major_filter_enabled else 'OFF'} · "
            f"version=`{getattr(state, 'major_filter_version', None) or macd2_config.MAJOR_FILTER_VERSION}`"
        )

b1, b2, b3, b4 = st.columns(4)
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
    if st.button("자동매매 중지 및 일괄매도", use_container_width=True):
        res = service.stop_and_liquidate_all("user_stop_liquidate_all")
        sold = [r for r in res.get("results", []) if r.get("symbol")]
        if res.get("ok"):
            if sold:
                detail = ", ".join(f"{r['symbol']} {r['quantity']}주" for r in sold)
                st.warning(f"자동매매 중지 + 일괄매도 완료: {detail}")
            else:
                st.warning("자동매매 중지됨 (보유 포지션 없음)")
        else:
            failed = ", ".join(f"{r.get('symbol') or '?'}:{r.get('block_reason')}" for r in sold if not r.get("ok"))
            st.error(f"일괄매도 일부/전체 실패 — {failed or res.get('message')}")
        st.rerun()
with b4:
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

    st.markdown("**Major filter (강한 플래그)**")
    mf1, mf2, mf3, mf4 = st.columns(4)
    mf1.metric("Major filter", "ON" if getattr(state, "major_filter_enabled", False) else "OFF")
    mf2.metric("filter version", getattr(state, "major_filter_version", None) or macd2_config.MAJOR_FILTER_VERSION)
    mf3.metric(
        "오늘 MAJOR 승인 진입",
        f"{int(getattr(state, 'daily_major_entry_count', 0) or 0)} / {macd2_config.MAJOR_MAX_DAILY_ENTRIES}",
    )
    mf4.metric("마지막 MAJOR 승인 시각", _format_signal_time(getattr(state, "last_major_entry_at", None)) if getattr(state, "last_major_entry_at", None) else "-")
    st.caption(
        f"enabled_at=`{getattr(state, 'major_filter_enabled_at', None) or '-'}` · "
        f"by=`{getattr(state, 'major_filter_enabled_by', None) or '-'}`"
    )

    st.markdown("**현재 confirmed 신호 / MAJOR 판정**")
    ms1, ms2, ms3, ms4 = st.columns(4)
    ms1.metric("원본 flag", state.latest_primary_flag.value if state.latest_primary_flag else "-")
    _score = getattr(state, "last_major_score", None)
    _req = getattr(state, "last_major_required_score", None)
    ms2.metric(
        "major score / required",
        f"{_score:.0f} / {_req:.0f}" if _score is not None and _req is not None else "-",
    )
    _approved = getattr(state, "last_major_approved", None)
    if _approved is True:
        _maj_status = "APPROVED"
    elif _approved is False:
        _maj_status = "FILTERED_OUT"
    else:
        _maj_status = "-"
    ms3.metric("MAJOR 결과", _maj_status)
    ms4.metric("block reason", getattr(state, "last_major_block_reason", None) or "-")
    st.caption(
        f"decision=`{getattr(state, 'last_major_decision', None) or '-'}` · "
        f"signal_id=`{getattr(state, 'last_major_signal_id', None) or state.latest_primary_signal_id or '-'}` · "
        f"components=`{getattr(state, 'last_major_component_scores', None) or '-'}`"
    )

    st.markdown("**Primary (완성봉 MACD color flag — 유일한 주문권한)**")
    st.caption("아래 diff/MACD는 진행 중(미완성) 3분봉의 shadow 진단값이며 주문에 사용되지 않는다. 실제 주문권한은 latest_primary_flag(완성봉)에만 있다.")
    pc1, pc2, pc3, pc4 = st.columns(4)
    pc1.metric("MACD (진행봉 shadow)", f"{state.provisional_macd:.6f}" if state.provisional_macd is not None else "-")
    pc2.metric("Signal (진행봉 shadow)", f"{state.provisional_signal:.6f}" if state.provisional_signal is not None else "-")
    prev_diff = state.primary_current_diff
    curr_diff = state.provisional_diff
    pc3.metric("previous diff (완성봉)", f"{prev_diff:.6f}" if prev_diff is not None else "-")
    pc4.metric("current diff (진행봉 shadow)", f"{curr_diff:.6f}" if curr_diff is not None else "-")
    fc1, fc2, fc3, fc4 = st.columns(4)
    fc1.metric(
        "candidate flag (진행봉, 주문권한 없음)",
        f"CANDIDATE_{state.candidate_flag.value}" if state.candidate_flag else "-",
    )
    fc2.metric("candidate confirmed (여전히 shadow)", state.provisional_flag.value if state.provisional_flag else "-")
    fc3.metric("last confirmed onset (완성봉, 주문권한)", state.latest_primary_flag.value if state.latest_primary_flag else "-")
    fc4.metric("broker order result", _broker_order_result_display(state))
    st.caption(
        f"current signal_id=`{state.provisional_signal_id or '-'}` · "
        f"last primary onset signal_id=`{state.latest_primary_signal_id or '-'}` · "
        f"candidate since=`{state.candidate_first_seen_at or '-'}` (first diff `{state.candidate_first_diff if state.candidate_first_diff is not None else '-'}`) · "
        f"last confirmed at=`{state.candidate_confirmed_at or '-'}` (confirmed diff `{state.candidate_confirmed_diff if state.candidate_confirmed_diff is not None else '-'}`)"
    )
    st.caption(
        f"broker order: id=`{state.last_broker_order_id or '-'}` · "
        f"symbol=`{state.last_broker_order_symbol or '-'}` · side=`{state.last_broker_order_side or '-'}` · "
        f"at=`{state.last_broker_order_at or '-'}`"
    )
    if _broker_order_result_display(state) == "LEGACY_ORDER_REJECTED_PRE_FIX":
        st.caption(
            "legacy broker message from a pre-fix order is hidden from the current status; "
            "new attempts use KIS nrcvb_buy_qty/final_qty diagnostics below."
        )
    st.caption(
        f"order failure detail: stage=`{state.last_order_failure_stage or '-'}` · "
        f"filled_qty=`{state.last_order_filled_qty if state.last_order_filled_qty is not None else '-'}` · "
        f"fill_poll_result=`{state.last_order_fill_poll_result or '-'}` · "
        f"balance_qty=`{state.last_order_balance_qty if state.last_order_balance_qty is not None else '-'}`"
    )
    if state.last_duplicate_signal_id:
        st.warning(f"signal ledger 중복 기록 거부됨 — signal_id=`{state.last_duplicate_signal_id}` (이미 기록된 signal_id)")
    st.markdown("**주문 sizing (직전 진입/스위치 시도)**")
    if state.last_order_orderable_cash is not None:
        oc1, oc2, oc3, oc4 = st.columns(4)
        oc1.metric("orderable_cash", f"{state.last_order_orderable_cash:,.0f}")
        oc2.metric("nrcvb_buy_amt", f"{state.last_order_nrcvb_buy_amt:,.0f}" if state.last_order_nrcvb_buy_amt is not None else "-")
        oc3.metric("nrcvb_buy_qty", state.last_order_nrcvb_buy_qty if state.last_order_nrcvb_buy_qty is not None else "-")
        oc4.metric("final_qty", state.last_order_final_qty if state.last_order_final_qty is not None else "-")
        oc5, oc6, oc7, oc8 = st.columns(4)
        oc5.metric("budget_qty", state.last_order_budget_qty if state.last_order_budget_qty is not None else "-")
        oc6.metric("psbl_qty_calc_unpr", f"{state.last_order_psbl_qty_calc_unpr:,.0f}" if state.last_order_psbl_qty_calc_unpr is not None else "-")
        oc7.metric("order_price", f"{state.last_order_order_price:,.2f}" if state.last_order_order_price is not None else "-")
        oc8.metric("expected_amount", f"{state.last_order_expected_amount:,.0f}" if state.last_order_expected_amount is not None else "-")
        oc9, oc10, oc11, oc12 = st.columns(4)
        oc9.metric("ask1", f"{state.last_order_ask1:,.2f}" if state.last_order_ask1 is not None else "-")
        oc10.metric("order_type", state.last_order_order_type or "-")
        oc11.metric("usable_cash", f"{state.last_order_usable_cash:,.0f}" if state.last_order_usable_cash is not None else "-")
        oc12.metric("limit_buyable_qty", state.last_order_limit_buyable_qty if state.last_order_limit_buyable_qty is not None else "-")
        st.caption(
            f"KIS sizing response: rt_cd=`{state.last_order_sizing_rt_cd or '-'}` · "
            f"msg_cd=`{state.last_order_sizing_msg_cd or '-'}` · msg1=`{state.last_order_sizing_msg1 or '-'}`"
        )
    else:
        st.caption("orderable_cash=`-` · nrcvb_buy_qty=`-` · budget_qty=`-` · final_qty=`-` · expected_amount=`-`")

    st.markdown("**QUOTE_STALE 재조회 진단 (2026-07-27)**")
    st.caption(
        f"signal_id=`{state.last_quote_stale_signal_id or '-'}` · "
        f"quote_ages(감지 시점)=`{state.last_quote_stale_quote_ages or '-'}` · "
        f"재조회 횟수=`{state.last_quote_stale_retry_count if state.last_quote_stale_retry_count is not None else '-'}` · "
        f"결과=`{state.last_quote_stale_result or '-'}`"
    )
    if state.last_quote_stale_result == macd2_config.MISSED_SIGNAL_QUOTE_STALE:
        st.warning(
            f"signal_id=`{state.last_quote_stale_signal_id}`은 15초 내 fresh quote를 확보하지 못해 "
            f"주문하지 않고 `{macd2_config.MISSED_SIGNAL_QUOTE_STALE}`로 종료되었습니다."
        )

    st.markdown("**1분봉 history 진단 (KIS 당일 1분봉 = 단일 원본)**")
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("당일 1분봉 수", state.today_1m_bar_count if state.today_1m_bar_count is not None else "-")
    h2.metric("history 최신시각", _format_signal_time(state.history_newest_at) if state.history_newest_at else "-")
    h3.metric("마지막 완성 3분봉", _format_signal_time(state.last_completed_3m_bar_at) if state.last_completed_3m_bar_at else "-")
    h4.metric("quote-history 불일치", state.quote_history_mismatch_reason or "OK")
    if state.quote_history_mismatch_reason:
        st.error(f"quote/1분봉 history 단위·시각 불일치로 신규 진입이 차단됩니다 — 원인: `{state.quote_history_mismatch_reason}`")

    st.markdown("**Provisional forming-bar crossover**")
    pr1, pr2, pr3, pr4 = st.columns(4)
    pr1.metric("MACD", f"{state.provisional_macd:.6f}" if state.provisional_macd is not None else "-")
    pr2.metric("Signal", f"{state.provisional_signal:.6f}" if state.provisional_signal is not None else "-")
    pr3.metric("diff", f"{state.provisional_diff:.6f}" if state.provisional_diff is not None else "-")
    pr4.metric("flag", state.provisional_flag.value if state.provisional_flag else "-")
    st.caption(
        f"forming=`{state.provisional_bar_start or '-'}` -> `{state.provisional_bar_end or '-'}` · "
        f"signal_id=`{state.provisional_signal_id or '-'}` · "
        f"detected_at=`{state.provisional_detected_at or '-'}` · "
        f"order_requested_at=`{state.provisional_order_requested_at or '-'}` · "
        f"input_now=`{getattr(state, 'provisional_input_now', None) or '-'}` · "
        f"quote=`{getattr(state, 'provisional_quote_price', None) or '-'}` · "
        f"last_1m=`{getattr(state, 'provisional_last_1m_at', None) or '-'}`/"
        f"`{getattr(state, 'provisional_last_1m_close', None) or '-'}` · "
        f"scale=`{getattr(state, 'provisional_price_scale_note', None) or '-'}`"
    )

    st.markdown("**Signed-B shadow**")
    st.caption(
        f"hist_last3=`{state.signed_b_shadow_hist_last3 or '-'}` · "
        f"signed-B=`{state.signed_b_shadow_direction.value if state.signed_b_shadow_direction else '-'}` · "
        "order_authority=`NONE`"
    )
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
    # docs §2: only THIS deployed code's SHA counts toward "current" stats —
    # a row from a different (older) worker_code_sha is excluded here and
    # only ever shown in the "과거/제외 신호" panel below.
    _current_worker_sha = snapshot.get("worker_code_sha") or None
    sig_summary = ledger.summarize_signals(
        trading_date,
        strategy_version=state.strategy_version,
        signal_rule=state.signal_rule,
        session_started_at=state.session_started_at,
        worker_code_sha=_current_worker_sha,
    )
    confirmed_summary = ledger.summarize_signals(
        trading_date,
        strategy_version=state.strategy_version,
        signal_rule=getattr(macd2_config, "CONFIRMED_SIGNAL_RULE", "KIS_MACD_COLOR_FLAG_CONFIRMED"),
        session_started_at=state.session_started_at,
        worker_code_sha=_current_worker_sha,
    )
    trade_summary = ledger.summarize_daily_trading(
        trading_date,
        budget=state.budget,
    )

    g1, g2, g3 = st.columns(3)
    g1.metric("오늘 빨간 플래그", f"{sig_summary['red_count']}건")
    g2.metric("오늘 파란 플래그", f"{sig_summary['blue_count']}건")
    g3.metric("완료 거래", f"{trade_summary['round_trip_count']}건")

    # MAJOR filter stats (원본 flag vs 승인 분리)
    _all_today = [r for r in ledger.load_signal_ledger() if r.get("trading_date") == trading_date]
    _onset = sig_summary.get("onset_signals") or []
    _major_approved_rows = [
        r for r in _onset
        if str(r.get("major_approved") or "").lower() in {"true", "1", "yes"}
        or str(r.get("major_decision") or "") == macd2_config.MAJOR_APPROVED
    ]
    _major_red = sum(1 for r in _major_approved_rows if r.get("direction") == "UP_RED")
    _major_blue = sum(1 for r in _major_approved_rows if r.get("direction") == "DOWN_BLUE")
    _filtered = [
        r for r in _onset
        if str(r.get("order_result") or "").upper() == macd2_config.FILTERED_OUT
        or str(r.get("major_approved") or "").lower() in {"false", "0", "no"}
    ]
    _filled_entries = int(trade_summary.get("buy_count") or 0)
    mg1, mg2, mg3, mg4, mg5, mg6 = st.columns(6)
    mg1.metric("원본 빨간 플래그", sig_summary["red_count"])
    mg2.metric("원본 파란 플래그", sig_summary["blue_count"])
    mg3.metric("MAJOR 승인 빨강", _major_red)
    mg4.metric("MAJOR 승인 파랑", _major_blue)
    mg5.metric("필터 탈락", len(_filtered))
    mg6.metric("실제 체결 진입", _filled_entries)

    if _filtered:
        st.markdown("**필터 탈락 신호**")
        _filt_rows = []
        for row in _filtered:
            _filt_rows.append({
                "시간": _signal_display_time(row),
                "방향": row.get("direction") or "-",
                "score": row.get("major_score") or "-",
                "required": row.get("major_required_score") or "-",
                "block reason": row.get("major_block_reason") or row.get("block_reason") or "-",
                "components": row.get("major_component_scores") or "-",
            })
        st.dataframe(pd.DataFrame(_filt_rows), use_container_width=True, height=220)

    st.caption(
        "KIS manual arrows today=- · "
        f"system provisional red/blue={sig_summary['red_count']}/{sig_summary['blue_count']} · "
        f"system confirmed red/blue={confirmed_summary['red_count']}/{confirmed_summary['blue_count']}"
    )
    onset_rows = sig_summary.get("onset_signals") or []
    if onset_rows:
        flag_rows = []
        for row in onset_rows:
            requested_at = _order_requested_at(row)
            flag_rows.append({
                "flag_time": _signal_display_time(row),
                "direction": row.get("direction") or "-",
                "signal_id": row.get("signal_id") or "-",
                "order_requested": "YES" if requested_at != "-" else "NO",
                "order_requested_at": requested_at,
                "order_result": str(row.get("order_result") or "-"),
                "not_ordered_reason": _not_ordered_reason(row, requested_at),
                "failure_stage": row.get("failure_stage") or "-",
                "orderable_cash": row.get("orderable_cash") or "-",
                "nrcvb_buy_amt": row.get("nrcvb_buy_amt") or "-",
                "nrcvb_buy_qty": row.get("nrcvb_buy_qty") or "-",
                "psbl_qty_calc_unpr": row.get("psbl_qty_calc_unpr") or "-",
                "ask1": row.get("ask1") or "-",
                "order_price": row.get("order_price") or "-",
                "order_type": row.get("order_type") or "-",
                "usable_cash": row.get("usable_cash") or "-",
                "limit_buyable_qty": row.get("limit_buyable_qty") or "-",
                "budget_qty": row.get("budget_qty") or "-",
                "final_qty": row.get("final_qty") or "-",
                "sizing_price": row.get("sizing_price") or "-",
                "requested_qty": row.get("requested_qty") or "-",
                "expected_amount": row.get("expected_amount") or "-",
                "sizing_rt_cd": row.get("sizing_rt_cd") or "-",
                "sizing_msg_cd": row.get("sizing_msg_cd") or "-",
                "sizing_msg1": row.get("sizing_msg1") or "-",
                "broker_called": row.get("broker_called") or "-",
                "rt_cd": row.get("broker_rt_cd") or "-",
                "msg_cd": row.get("broker_msg_cd") or "-",
                "msg1": row.get("broker_msg1") or "-",
                "order_id": row.get("broker_order_id") or "-",
                "filled_qty": row.get("filled_qty") or "-",
                "fill_poll_result": row.get("fill_poll_result") or "-",
                "balance_qty": row.get("balance_qty") or "-",
            })
        red_times = [r["flag_time"] for r in flag_rows if r["direction"] == "UP_RED"]
        blue_times = [r["flag_time"] for r in flag_rows if r["direction"] == "DOWN_BLUE"]
        st.caption(f"빨간 플래그 {len(red_times)}건: {', '.join(red_times) if red_times else '-'}")
        st.caption(f"파란 플래그 {len(blue_times)}건: {', '.join(blue_times) if blue_times else '-'}")
        st.dataframe(pd.DataFrame(flag_rows), use_container_width=True, height=260)
    else:
        st.caption("No current-version provisional flags today.")

    for u in sig_summary.get("unexecuted_signals") or []:
        st.write(f"- `{u.get('signal_id')}` · {u.get('direction')} · 사유 `{u.get('reason')}`")
    excluded = sig_summary.get("excluded_signals") or []
    if excluded:
        with st.expander(f"과거/제외 신호 ({len(excluded)}건)"):
            st.dataframe(pd.DataFrame(excluded), use_container_width=True, height=260)

    # docs §3: recomputed today-overview, LIVE_CONFIRMED vs
    # HISTORICAL_REPLAY_ONLY — display only, never an order/filter input.
    _overview = snapshot.get("today_signal_overview") or []
    _live = [r for r in _overview if r.get("origin") == "LIVE_CONFIRMED"]
    _historical = [r for r in _overview if r.get("origin") == "HISTORICAL_REPLAY_ONLY"]
    st.markdown("**오늘 전체 신호 개요 (재계산, 참고용 — 주문권한 없음)**")
    ov1, ov2 = st.columns(2)
    ov1.metric("LIVE_CONFIRMED (Worker 시작 이후)", f"{len(_live)}건")
    ov2.metric("HISTORICAL_REPLAY_ONLY (Worker 시작 전)", f"{len(_historical)}건")
    if _overview:
        st.dataframe(pd.DataFrame(_overview), use_container_width=True, height=200)

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
    today_rows = [r for r in rows if str(r.get("timestamp") or "").startswith(trading_date)]
    st.caption(
        f"execution ledger path=`{ledger.EXECUTION_LEDGER_PATH}` · "
        f"loaded_rows={len(rows)} · today_rows={len(today_rows)}"
    )
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df.iloc[::-1], use_container_width=True, height=360)
    else:
        st.caption("원장 없음")
except Exception as exc:
    st.error(f"원장 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")
