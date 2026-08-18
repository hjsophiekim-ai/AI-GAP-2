"""
11_MACD_자동매매2.py — ReadOnly UI for MACD2 (독립 신규 모듈)

MACD2는 app/trading/macd2/* 로 완전히 독립되어 있으며, 다른 자동매매
엔진 코드를 호출하지 않는다. UI는 command 기록(시작/중지)과 service.get_snapshot()
읽기만 수행한다 — MACD 계산·network 호출·Worker 생성/reload를 UI에서
직접 하지 않는다(docs/MACD2_LOGIC.md §16).

2026-08-18 사용자 요청: 화면이 너무 지저분해서 MU_MACD 페이지(12_MU_MACD_
자동매매.py)와 동일한 수준으로 간결하게 재구성 — 필터는 "시간대별 최적거래
필터"와 "퀵 Profit 익절"만 남기고, 그 외 필터(강한 플래그/추세전환장/Trend
Persistence/2% 3회진입/Profit Lock)의 토글과 진단 패널, 그리고 깊은 디버그용
진단 패널(운영 진단/데이터 저장 경로/주문 sizing/QUOTE_STALE/1분봉 history/
Provisional forming-bar/Signed-B shadow/필터 탈락 신호 등)은 화면에서 뺐다.
숨긴 필터들의 백엔드 로직/상태 필드/service 메서드는 전혀 건드리지 않았다 —
그 필터가 이미 state 파일에 ON으로 저장되어 있다면 그 설정 그대로 계속
동작한다(단, UI로는 더 이상 켜고 끌 수 없다 — 필요하면 .env로 제어).
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
from app.trading.macd2.service import get_service  # noqa: E402

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
worker_stats = snapshot["worker"] or {}
quotes = snapshot["quotes"] or {}

col1, col2, col3 = st.columns(3)
col1.metric("Worker 상태", _worker_status(state, worker_stats))
col2.metric("모드", state.mode)
col3.metric("예산", f"{state.budget:,.0f}원")

st.subheader("계좌 / 제어")
c1, c2, c3 = st.columns([1.2, 1.2, 1])
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

st.caption("수동 진입 (MACD 신호·필터 무시, 예산 내 즉시 전량매수 — 이미 보유 중이면 거부)")
m1, m2 = st.columns(2)
with m1:
    if st.button("현재시점 레버리지(레드) 전량매수", use_container_width=True):
        res = service.manual_entry("UP_RED")
        if res.get("ok"):
            st.success(f"레버리지 매수 체결: {res.get('symbol')} {res.get('quantity')}주 @ {res.get('price')}")
        else:
            st.error(f"레버리지 매수 실패: {res.get('message') or res.get('block_reason')}")
        st.rerun()
with m2:
    if st.button("현재시점 인버스(블루) 전량매수", use_container_width=True):
        res = service.manual_entry("DOWN_BLUE")
        if res.get("ok"):
            st.success(f"인버스 매수 체결: {res.get('symbol')} {res.get('quantity')}주 @ {res.get('price')}")
        else:
            st.error(f"인버스 매수 실패: {res.get('message') or res.get('block_reason')}")
        st.rerun()

_sched_dir = getattr(state, "scheduled_entry_armed_direction", None)
_sched_done = getattr(state, "scheduled_entry_executed_at", None)
if _sched_done:
    _protect_note = (
        f" · 반대 플래그 보호 중(~{macd2_config.SCHEDULED_ENTRY_PROTECTION_UNTIL.strftime('%H:%M')}까지 반대신호청산 무시)"
        if getattr(state, "scheduled_entry_protected", False) else ""
    )
    st.caption(f"09:03 예약 매수 — 오늘 처리 완료: `{state.scheduled_entry_last_result or '-'}`{_protect_note}")
else:
    _sched_label = "레버리지(레드)" if (_sched_dir and _sched_dir.value == "UP_RED") else (
        "인버스(블루)" if (_sched_dir and _sched_dir.value == "DOWN_BLUE") else "없음"
    )
    st.caption(f"09:03 예약 매수 (개장 직후 이른 플래그 대응, 하루 1회) — 현재 예약: {_sched_label}")
sch1, sch2 = st.columns(2)
with sch1:
    _armed_up = bool(_sched_dir and _sched_dir.value == "UP_RED")
    if st.button(
        ("[예약중] " if _armed_up else "") + "09시03분 레버리지(레드) 전량매수 예약",
        use_container_width=True, disabled=bool(_sched_done),
    ):
        res = service.arm_scheduled_entry("UP_RED")
        if res.get("ok"):
            st.success("09:03 레버리지 전량매수 예약됨" if res.get("armed") else "예약 해제됨")
        else:
            st.error(res.get("message") or "예약 실패")
        st.rerun()
with sch2:
    _armed_down = bool(_sched_dir and _sched_dir.value == "DOWN_BLUE")
    if st.button(
        ("[예약중] " if _armed_down else "") + "09시03분 인버스(블루) 전량매수 예약",
        use_container_width=True, disabled=bool(_sched_done),
    ):
        res = service.arm_scheduled_entry("DOWN_BLUE")
        if res.get("ok"):
            st.success("09:03 인버스 전량매수 예약됨" if res.get("armed") else "예약 해제됨")
        else:
            st.error(res.get("message") or "예약 실패")
        st.rerun()

st.caption("수동 전량매도 (자동매매는 계속 유지, 현재 보유 포지션만 지금 즉시 매도)")
if st.button("현재 보유 포지션 수동 전량매도", use_container_width=True):
    res = service.manual_exit()
    if res.get("ok"):
        st.success(f"수동 매도 체결: {res.get('symbol')} {res.get('quantity')}주 @ {res.get('price')}")
    else:
        st.error(f"수동 매도 실패: {res.get('message') or res.get('block_reason')}")
    st.rerun()

_qp_cols = st.columns([1.4, 1.6])
with _qp_cols[0]:
    _qp_on = st.checkbox(
        "퀵 Profit 익절",
        value=bool(getattr(state, "quick_profit_enabled", False)),
        key="macd2_quick_profit_toggle",
        help=(
            f"ON이면 보유 포지션 순수익률이 +{macd2_config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT}%에 도달하는 즉시 전량 익절"
            "(진입 로직과 무관하게 동일 적용, 기존 손절·반대플래그청산·강제청산은 그대로 유지). 기본 OFF."
        ),
    )
with _qp_cols[1]:
    if bool(_qp_on) != bool(getattr(state, "quick_profit_enabled", False)):
        res = service.set_quick_profit_enabled(bool(_qp_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"퀵 Profit 익절 → {'ON' if _qp_on else 'OFF'}")
            st.rerun()
        else:
            st.error("퀵 Profit 익절은 Profit Lock과 동시에 켤 수 없습니다.")
    else:
        st.caption(f"퀵 Profit 익절={'ON' if state.quick_profit_enabled else 'OFF'} · 문턱=+{macd2_config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT}%")

_tw_cols = st.columns([1.4, 1.6])
with _tw_cols[0]:
    _tw_on = st.checkbox(
        "시간대별 최적거래 필터",
        value=bool(getattr(state, "time_window_filter_enabled", False)),
        key="macd2_time_window_filter_toggle",
        help=(
            "켜면 완성봉 플래그가 뜬 즉시 진입하지 않고 다음 완성 3분봉(T+3)에서 방향유지+gap확대를 재확인한 뒤에만 "
            f"진입하며, 09:00-13:00 구간 품질점수 게이트(오전만, 13:00 이후 신규진입 없음)와 손절 "
            f"{macd2_config.MORNING_STOP_LOSS*100:.1f}%/TP1 +{macd2_config.MORNING_TP1*100:.1f}%"
            f"({macd2_config.MORNING_TP1_SELL_RATIO*100:.0f}%)/TP2 +{macd2_config.MORNING_TP2*100:.1f}% 래더로 관리합니다. 기본 OFF."
        ),
    )
with _tw_cols[1]:
    if bool(_tw_on) != bool(getattr(state, "time_window_filter_enabled", False)):
        res = service.set_time_window_filter_enabled(bool(_tw_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"시간대별 최적거래 필터 → {'ON' if _tw_on else 'OFF'}")
            st.rerun()
    else:
        st.caption(
            f"시간대별 최적거래 필터={'ON' if state.time_window_filter_enabled else 'OFF'} · "
            f"오전 진입 {int(getattr(state, 'time_window_morning_entry_count', 0) or 0)}/{macd2_config.MAX_MORNING_ENTRIES} · "
            f"오후 진입 {int(getattr(state, 'time_window_afternoon_entry_count', 0) or 0)}/{macd2_config.MAX_AFTERNOON_ENTRIES} · "
            f"포지션관리 활성={'Y' if getattr(state, 'time_window_position_active', False) else '-'}"
        )

_dbe_cols = st.columns([1.4, 1.6])
with _dbe_cols[0]:
    _dbe_on = st.checkbox(
        "└ TW 1 blue",
        value=bool(getattr(state, "down_blue_exception_filter_enabled", False)),
        key="macd2_down_blue_exception_toggle",
        disabled=not bool(state.time_window_filter_enabled),
        help=(
            "시간대별 최적거래 필터가 거절한 DOWN_BLUE 플래그 중, 다른 조건 없이 하루 최대 1회만 추가로 진입합니다. "
            "56거래일 TRAIN/VAL/OOS 백테스트에서 조건 없이 그대로 허용하는 쪽이 세 구간 모두 일관되게 개선되어 채택됨"
            "(연쇄복리 69.3%→105.3%). 시간대별 최적거래 필터가 꺼져있으면 효과 없음. 기본 OFF."
        ),
    )
with _dbe_cols[1]:
    if bool(_dbe_on) != bool(getattr(state, "down_blue_exception_filter_enabled", False)):
        res = service.set_down_blue_exception_filter_enabled(bool(_dbe_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"TW 1 blue → {'ON' if _dbe_on else 'OFF'}")
            st.rerun()
    else:
        st.caption(
            f"TW 1 blue={'ON' if state.down_blue_exception_filter_enabled else 'OFF'} · "
            f"오늘 사용={'Y' if getattr(state, 'daily_down_blue_exception_used', False) else '-'}"
        )

# Re-read after potential command
snapshot = service.get_snapshot()
state = snapshot["state"]
worker_stats = snapshot["worker"] or {}
quotes = snapshot["quotes"] or {}
bootstrap_last_result = snapshot.get("bootstrap_last_result")

st.subheader("시세 / Bootstrap 상태")
quote_status = snapshot.get("quote_status") or _quote_status(quotes)
bootstrap_status = _bootstrap_status(state, bootstrap_last_result)
w1, w2, w3, w4 = st.columns(4)
w1.metric("quote_status", quote_status)
w2.metric("bootstrap_status", bootstrap_status)
w3.metric("웜업 완료", "YES" if state.warmup_ready else "NO")
w4.metric("전략 상태", state.ui_mode.value)
if state.order_block_reason:
    st.write(f"최근 block/skip 사유: `{state.order_block_reason}`")

st.subheader("현재 신호 / 포지션")
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

s1, s2 = st.columns(2)
s1.metric("마지막 확정 플래그", state.latest_primary_flag.value if state.latest_primary_flag else "-")
if state.position:
    s2.metric("보유 종목", f"{state.position.symbol} · {state.position.quantity}주 · 평단 {state.position.avg_price:,.0f}")
else:
    s2.metric("보유 종목", "flat")

st.subheader("신호 원장 (오늘, 최근 100건)")
trading_date = state.session_date or pd.Timestamp.now().strftime("%Y%m%d")
signal_rows = [r for r in ledger.load_signal_ledger(limit=2000) if r.get("trading_date") == trading_date][-100:]
if signal_rows:
    st.dataframe(pd.DataFrame(signal_rows), use_container_width=True)
else:
    st.caption("오늘 기록된 신호가 없습니다.")

st.subheader("체결 원장 (오늘, 최근 100건)")
exec_rows_all = ledger.load_execution_ledger(limit=2000)
exec_rows = ledger.filter_execution_rows_by_trading_date(exec_rows_all, trading_date)[-100:]
if exec_rows:
    st.dataframe(pd.DataFrame(exec_rows), use_container_width=True)
else:
    st.caption("오늘 기록된 체결이 없습니다.")

with st.expander("전략 설명"):
    st.markdown(
        f"""
- **신호**: SK하이닉스(000660) 3분봉 MACD({macd2_config.EMA_FAST},{macd2_config.EMA_SLOW},{macd2_config.EMA_SIGNAL}) confirmed crossover.
- **방향→매수**: RED → {macd2_config.LONG_SYMBOL}(레버리지), BLUE → {macd2_config.INVERSE_SYMBOL}(인버스).
- **반대 플래그**: 보유 포지션 전량매도 후 반대 ETF 매수(entry_gate 통과 시에만 재매수, 매도는 항상 실행).
- **리스크**: {macd2_config.FORCE_LIQUIDATE_AT} 강제청산 — 매 tick마다 플래그 발생 여부와 무관하게 확인.
- **퀵 Profit 익절(옵션, 기본 OFF)**: ON이면 순수익률이 +{macd2_config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT}%에 도달하는 즉시 전량 익절 — 확정 플래그와 무관하게 매 tick 확인.
- **시간대별 최적거래 필터(옵션, 기본 OFF)**: ON이면 다른 진입 로직 대신 이 필터가 진입권한 + 포지션 관리(TP1/TP2/손절 래더)를 모두 담당 — 완성봉 플래그 확정 후 다음 완성 3분봉(T+3)에서 재확인해야만 진입.
- **└ TW 1 blue(옵션, 기본 OFF, 시간대별 최적거래 필터 하위)**: ON이면 위 필터가 거절한 DOWN_BLUE 플래그 중 하루 최대 1회만 다른 조건 없이 추가로 진입.
- **09:03 예약 매수(옵션)**: 개장 직후 데이터 부족으로 이른 플래그를 놓치는 문제 대응 — 미리 예약해두면 09:03에 지정 방향 ETF를 자동으로 전량매수(하루 1회).
        """
    )
