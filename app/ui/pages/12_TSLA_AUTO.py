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

import json
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


def _signal_display_time(row: dict) -> str:
    return _format_dual_time({"et": row.get("bar_start_at_et"), "kst": row.get("bar_start_at_kst")})


def _order_requested_at(row: dict) -> str:
    return _format_dual_time({"et": row.get("order_requested_at_et"), "kst": row.get("order_requested_at_kst")})


def _not_ordered_reason(row: dict, requested_at: str) -> str:
    if requested_at != "-":
        return "-"
    for key in ("block_reason", "final_result", "order_result"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "NO_ORDER_REQUEST"


def _as_float(value: object) -> float | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except Exception:
        return None


def _as_bool(value: object) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _fmt_num(value: object, digits: int = 2) -> str:
    num = _as_float(value)
    return "-" if num is None else f"{num:.{digits}f}"


def _trade_entered_status(row: dict, requested_at: str) -> str:
    result = str(row.get("order_result") or "").strip().upper()
    if result == "EXECUTED":
        return "YES"
    if requested_at != "-":
        return "ORDER_REQUESTED"
    return "NO"


def _load_strong_metrics(row: dict) -> dict:
    text = str(row.get("strong_metrics") or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _strong_unmet_summary(row: dict) -> tuple[str, str]:
    enabled = _as_bool(row.get("strong_filter_enabled"))
    decision = str(row.get("strong_decision") or "").strip()
    approved = _as_bool(row.get("strong_approved"))
    if enabled is False:
        return "OFF", "filter OFF"
    if not decision and approved is None:
        return "-", "-"
    if approved is True or decision == tsla_config.STRONG_APPROVED:
        return "PASS", "충족"

    metrics_d = _load_strong_metrics(row)
    reason = str(metrics_d.get("strong_profile_reason") or row.get("strong_block_reason") or row.get("block_reason") or decision or "").strip()
    score = _fmt_num(row.get("strong_score"), 0)
    required = _fmt_num(row.get("strong_required_score"), 0)
    price = _fmt_num(metrics_d.get("price_impulse_atr"))
    hist = _fmt_num(metrics_d.get("hist_impulse_atr"))
    volume = _fmt_num(metrics_d.get("volume_ratio"))
    trend = str(metrics_d.get("ema20_or_vwap_ok") if "ema20_or_vwap_ok" in metrics_d else "-")
    detail = f"score {score}/{required}, price {price}ATR, hist {hist}, vol {volume}, trend {trend}"

    if decision == tsla_config.STRONG_SCORE_BELOW_THRESHOLD:
        return "FAIL", f"score 미달 ({score} < {required})"
    if decision == tsla_config.STRONG_PRICE_CONFIRMATION_FAILED:
        return "FAIL", f"price confirmation 미달 ({detail})"
    if decision == tsla_config.STRONG_SIDEWAYS_BLOCK:
        return "FAIL", f"횡보 차단 ({detail})"
    if decision == tsla_config.STRONG_PROFILE_FAILED:
        return "FAIL", f"{reason or 'V6 profile 미일치'} ({detail})"
    if decision == "DAILY_ENTRY_LIMIT":
        return "FAIL", "일일 진입 한도"
    if decision == tsla_config.STRONG_SAME_DIRECTION_COOLDOWN:
        return "FAIL", "동일방향 재진입 쿨다운"
    if decision == tsla_config.STRONG_MIN_HOLD_BLOCK:
        return "FAIL", "최소 보유시간 미충족"
    if decision == tsla_config.SAME_DIRECTION_POSITION_HELD:
        return "FAIL", "이미 같은 방향 보유"
    return "FAIL", f"{reason or decision or 'V6 조건 미충족'} ({detail})"


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

us_market_state = snapshot.get("us_market_state") or {}
session_status = us_market_state.get("phase") or market_session.classify_session_status()
m0, m1, m2 = st.columns(3)
m0.metric("READ_ONLY/MOCK/REAL", state.mode)
m1.metric("전략 상태", state.ui_mode.value)
m2.metric("미국 세션", session_status)

st.subheader("미국장 운영상태")
try:
    phase = us_market_state.get("phase") or "-"
    reason_text = us_market_state.get("reason_text_ko") or "-"
    if phase == "REGULAR_ENTRY":
        st.success(reason_text)
    elif phase in ("FORCE_LIQUIDATION", "CALENDAR_UNAVAILABLE"):
        st.error(reason_text)
    elif phase == "ENTRY_BLOCKED":
        st.warning(reason_text)
    else:
        st.info(reason_text)

    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("현재 미국시간", _format_dual_time({"et": us_market_state.get("checked_at_et")}))
    mc2.metric("현재 한국시간", _format_dual_time({"et": us_market_state.get("checked_at_kst")}))
    mc3.metric("서머타임 적용", f"{'예' if us_market_state.get('is_dst') else '아니오'} ({us_market_state.get('timezone_abbr') or '-'}, {us_market_state.get('utc_offset') or '-'})")
    mc4.metric("거래일", "YES" if us_market_state.get("is_trading_day") else "NO")

    mt1, mt2, mt3, mt4 = st.columns(4)
    mt1.metric("휴장/주말", "주말" if us_market_state.get("is_weekend") else ("휴장" if us_market_state.get("is_holiday") else "-"))
    mt2.metric("조기폐장", "YES" if us_market_state.get("is_early_close") else "NO")
    mt3.metric("신규진입", "가능" if us_market_state.get("entry_allowed") else "차단")
    mt4.metric("강제청산", "필요" if us_market_state.get("liquidation_required") else "-")

    ms_cols = st.columns(4)
    ms_cols[0].metric("정규장 개장", _format_dual_time({"et": us_market_state.get("session_open_et"), "kst": us_market_state.get("session_open_kst")}))
    ms_cols[1].metric("정규장 폐장", _format_dual_time({"et": us_market_state.get("session_close_et"), "kst": us_market_state.get("session_close_kst")}))
    ms_cols[2].metric("신규진입 차단", _format_dual_time({"et": us_market_state.get("entry_block_at_et"), "kst": us_market_state.get("entry_block_at_kst")}))
    ms_cols[3].metric("전 종목 강제청산", _format_dual_time({"et": us_market_state.get("liquidation_at_et"), "kst": us_market_state.get("liquidation_at_kst")}))

    countdown = us_market_state.get("seconds_to_next_transition")
    if countdown is not None:
        st.caption(f"다음 상태 전환까지: {int(countdown) // 60}분 {int(countdown) % 60}초")
    if us_market_state.get("next_open_et"):
        st.caption(f"다음 정규장 개장: {_format_dual_time({'et': us_market_state.get('next_open_et'), 'kst': us_market_state.get('next_open_kst')})}")
    liquidation_status = getattr(state, "liquidation_status", {}) or {}
    st.markdown("**강제청산 현황**")
    ls1, ls2, ls3 = st.columns(3)
    symbols_status = liquidation_status.get("symbols") or {}
    remaining_symbols = liquidation_status.get("remaining_symbols") or [
        symbol for symbol, meta in symbols_status.items() if int((meta or {}).get("remaining_qty") or 0) > 0
    ]
    ls1.metric("대상 종목 수", liquidation_status.get("target_count", len(symbols_status)))
    ls2.metric("완료 종목 수", liquidation_status.get("completed_count", len([m for m in symbols_status.values() if (m or {}).get("state") == "FLAT"])))
    ls3.metric("잔여 종목", ", ".join(remaining_symbols) if remaining_symbols else "-")
    failure_reason = liquidation_status.get("failure_reason") or "-"
    if failure_reason != "-":
        st.error(f"강제청산 실패 사유: {failure_reason}")
    if symbols_status:
        rows = []
        for symbol, meta in symbols_status.items():
            meta = meta or {}
            rows.append({
                "symbol": symbol,
                "state": meta.get("state") or "READY",
                "remaining_qty": meta.get("remaining_qty", 0),
                "attempts": meta.get("attempts", 0),
                "last_order_id": meta.get("last_order_id", ""),
                "failure_reason": meta.get("last_reason", ""),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, height=140)
except Exception as exc:
    st.error(f"미국장 운영상태 패널 오류: `{exc}`")

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

# Optional Quick-Profit take-profit exit toggle (MACD2 parity, 2026-08-04).
_qp_cols = st.columns([1.4, 1.6])
with _qp_cols[0]:
    _qp_on = st.checkbox(
        "퀵 Profit 익절", value=bool(getattr(state, "quick_profit_enabled", False)),
        key="tsla_auto_quick_profit_toggle",
        help=f"보유 중 순손익률이 +{tsla_config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT}% 도달 시 즉시 전량 익절 (손절과 독립적인 별도 청산 로직)",
    )
with _qp_cols[1]:
    if bool(_qp_on) != bool(getattr(state, "quick_profit_enabled", False)):
        res = service.set_quick_profit_enabled(bool(_qp_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"퀵 Profit 익절 → {'ON' if _qp_on else 'OFF'} (`{res.get('quick_profit_enabled_at')}`)")
            st.rerun()
    else:
        st.caption(f"퀵 Profit 익절={'ON' if state.quick_profit_enabled else 'OFF'}")

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
    m2.metric("Total Cost USD", f"${trade_summary['total_cost_usd']:,.2f}")
    m3.metric("Net USD", f"${trade_summary['net_pnl_usd']:,.2f}")
    m4.metric("수익률", f"{trade_summary['return_pct']:.4f}%")
    m5.metric("승률", f"{trade_summary['win_rate_pct']:.1f}%")

    cst1, cst2, cst3 = st.columns(3)
    cst1.metric("Commission USD", f"${trade_summary.get('total_commission_usd', 0.0):,.2f}")
    cst2.metric("Slippage USD", f"${trade_summary.get('total_slippage_usd', 0.0):,.2f}")
    cst3.metric("FX Cost USD", f"${trade_summary.get('total_fx_cost_usd', 0.0):,.2f}")

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

    onset_rows = sig_summary.get("onset_signals") or []
    if onset_rows:
        st.markdown("**플래그별 주문 / 강한 필터 판정**")
        flag_rows = []
        for row in onset_rows:
            requested_at = _order_requested_at(row)
            strong_result, strong_unmet = _strong_unmet_summary(row)
            flag_rows.append({
                "flag_time": _signal_display_time(row),
                "direction": row.get("direction") or "-",
                "signal_id": row.get("signal_id") or "-",
                "entered": _trade_entered_status(row, requested_at),
                "strong_result": strong_result,
                "strong_unmet": strong_unmet,
                "strong_score": row.get("strong_score") or "-",
                "strong_required": row.get("strong_required_score") or "-",
                "strong_decision": row.get("strong_decision") or "-",
                "order_requested": "YES" if requested_at != "-" else "NO",
                "order_requested_at": requested_at,
                "order_result": str(row.get("order_result") or "-"),
                "not_ordered_reason": _not_ordered_reason(row, requested_at),
                "failure_stage": row.get("failure_stage") or "-",
                "order_id": row.get("broker_order_id") or "-",
                "filled_qty": row.get("filled_qty") or "-",
                "fill_poll_result": row.get("fill_poll_result") or "-",
                "balance_qty": row.get("balance_qty") or "-",
            })
        st.dataframe(pd.DataFrame(flag_rows), use_container_width=True, height=260)
    else:
        st.caption("No current-version live flags today.")

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
    today_rows = ledger.filter_execution_rows_by_trading_date(rows, trading_date)
    st.caption(f"execution ledger path=`{ledger.EXECUTION_LEDGER_PATH}` · loaded_rows={len(rows)} · today_rows={len(today_rows)}")
    if today_rows:
        df = pd.DataFrame(today_rows)
        st.dataframe(df.iloc[::-1], use_container_width=True, height=300)
    elif rows:
        st.caption("오늘 원장 없음")
    else:
        st.caption("원장 없음")
except Exception as exc:
    st.error(f"원장 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")

# ── Session boundaries diagnostics (isolated) ────────────────────────────
st.subheader("미국시장 세션 진단")
try:
    b1, b2, b3 = st.columns(3)
    b1.metric("정규장 개장", _format_dual_time({"et": us_market_state.get("session_open_et"), "kst": us_market_state.get("session_open_kst")}))
    b2.metric("정규장 폐장", _format_dual_time({"et": us_market_state.get("session_close_et"), "kst": us_market_state.get("session_close_kst")}))
    b3.metric("조기폐장 여부", "YES" if us_market_state.get("is_early_close") else "NO")
    b4, b5, b6 = st.columns(3)
    b4.metric("신규진입 차단", _format_dual_time({"et": us_market_state.get("entry_block_at_et"), "kst": us_market_state.get("entry_block_at_kst")}))
    b5.metric("강제청산 시작", _format_dual_time({"et": us_market_state.get("liquidation_at_et"), "kst": us_market_state.get("liquidation_at_kst")}))
    b6.metric("시장 phase", us_market_state.get("phase") or "-")
except Exception as exc:
    st.error(f"세션 진단 패널 오류 — 나머지 화면은 계속 표시됩니다 (`{exc}`)")
