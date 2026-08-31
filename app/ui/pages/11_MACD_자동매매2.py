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
from html import escape

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
    # 2026-08-24: mirrors market_data.MarketDataService.quote_status()'s
    # symbol set -- WATCH_SYMBOL(000660) is signal-source-only and never
    # gates an order, so it's excluded here too (else this fallback path
    # would disagree with the service's own badge value).
    for symbol in (macd2_config.LONG_SYMBOL, macd2_config.INVERSE_SYMBOL):
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


def _parse_flag_event_time(row: dict) -> datetime | None:
    completed_bar_at = str(row.get("completed_bar_at") or "")
    trading_date = str(row.get("trading_date") or "")
    if len(completed_bar_at) == 6 and completed_bar_at.isdigit() and len(trading_date) == 8 and trading_date.isdigit():
        try:
            return datetime.strptime(f"{trading_date}{completed_bar_at}", "%Y%m%d%H%M%S").replace(tzinfo=macd2_config.KST)
        except ValueError:
            pass
    for key in ("bar_start_at", "bar_end_at"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw)).astimezone(macd2_config.KST)
        except ValueError:
            pass
    signal_id = str(row.get("signal_id") or "")
    parts = signal_id.split("_")
    if len(parts) >= 2 and len(parts[1]) == 6 and parts[1].isdigit():
        if len(trading_date) == 8 and trading_date.isdigit():
            try:
                return datetime.strptime(f"{trading_date}{parts[1]}", "%Y%m%d%H%M%S").replace(tzinfo=macd2_config.KST)
            except ValueError:
                return None
    return None


def _latest_flag_event(rows: list[dict]) -> dict | None:
    timed_rows = [(_parse_flag_event_time(row), row) for row in rows]
    timed_rows = [(ts, row) for ts, row in timed_rows if ts is not None]
    if not timed_rows:
        return rows[-1] if rows else None
    return max(timed_rows, key=lambda item: item[0])[1]


def _hhmmss(raw: str) -> str:
    raw = str(raw or "")
    if len(raw) == 6 and raw.isdigit():
        return f"{raw[0:2]}:{raw[2:4]}:{raw[4:6]}"
    try:
        return datetime.fromisoformat(raw).astimezone(macd2_config.KST).strftime("%H:%M:%S")
    except ValueError:
        return raw


def _signal_flag_time(row: dict) -> str:
    if row.get("signal_bar_at"):
        return _hhmmss(row.get("signal_bar_at"))
    return _hhmmss(row.get("completed_bar_at"))


def _signal_confirm_time(row: dict) -> str:
    return _hhmmss(row.get("signal_confirmed_at") or row.get("detected_at"))


def _is_display_signal(row: dict) -> bool:
    signal_id = str(row.get("signal_id") or "")
    if signal_id.startswith("RECONCILE_DISCOVERED"):
        return False
    signal_type = str(row.get("signal_type") or "")
    return signal_type in {
        "INITIAL",
        "REVERSAL",
        "PREMARKET_CARRY_TW",
        "SCHEDULED_ENTRY_0903",
        "MANUAL_ENTRY",
        "MANUAL_LIQUIDATION",
    }


def _signal_label(row: dict) -> str:
    signal_type = str(row.get("signal_type") or "")
    if signal_type == "PREMARKET_CARRY_TW":
        return "프리마켓 승계"
    if signal_type == "SCHEDULED_ENTRY_0903":
        return "09:03 예약"
    if signal_type == "MANUAL_ENTRY":
        return "수동 진입"
    if signal_type == "MANUAL_LIQUIDATION":
        return "수동 청산"
    return "반대 플래그" if signal_type == "REVERSAL" else "플래그"


def _order_summary(row: dict) -> str:
    result = str(row.get("order_result") or row.get("final_result") or "NO_ORDER")
    reason = str(row.get("block_reason") or row.get("failure_stage") or "")
    broker_order_id = str(row.get("broker_order_id") or "")
    order_type = str(row.get("order_type") or "")
    price = row.get("order_price") or ""
    qty = row.get("filled_qty") or row.get("final_qty") or row.get("requested_qty") or ""
    pieces = [result]
    if broker_order_id:
        pieces.append(f"주문번호 {broker_order_id}")
    if qty:
        pieces.append(f"{qty}주")
    if price:
        pieces.append(f"@ {price}")
    if order_type:
        pieces.append(order_type)
    if reason:
        pieces.append(reason)
    return " / ".join(str(p) for p in pieces if str(p))


def _signal_timeline_rows(rows: list[dict]) -> list[dict]:
    timeline: list[dict] = []
    for row in rows:
        if not _is_display_signal(row):
            continue
        direction = str(row.get("direction") or row.get("confirmed_direction") or "")
        label = _signal_label(row)
        timeline.append({
            "구분": "플래그/확정",
            "시각": f"{_signal_flag_time(row)} -> {_signal_confirm_time(row)}",
            "내용": f"{label} {direction}".strip(),
        })
        timeline.append({
            "구분": "주문",
            "시각": _hhmmss(row.get("order_requested_at")) or "-",
            "내용": _order_summary(row),
        })
    return timeline


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

_teg_cols = st.columns([1.4, 1.6])
with _teg_cols[0]:
    _teg_on = st.checkbox(
        "+TEGv2",
        value=bool(getattr(state, "time_window_teg_filter_enabled", False)),
        key="macd2_time_window_teg_filter_toggle",
        disabled=not bool(getattr(state, "time_window_2_filter_enabled", False)),
        help=(
            "TW2와 완전히 동일한 T+3 재확인/품질점수/시간대/VWAP veto/최근크로스 veto 게이트+포지션관리 래더를 그대로 "
            "쓰되, 기존 TW2 하루 3회 진입한도 때문에만 거절된 후보에 한해 하루 1회 Trend Establishment Gate v2(TEGv2) 검증을 "
            "추가로 통과하면 한도를 넘어서도 진입합니다(무제한 아님, 하루 정확히 1회). TEGv2는 최근30분크로스<=1/MACD갭·"
            "EMA10-20스프레드 signed 순증가/가격-EMA스택 정렬/세션VWAP 유리한 쪽/직전반대플래그 9분 이상 경과를 모두 "
            "요구합니다. 2026-06-01~08-26 60거래일 TRAIN(40일, 임계값 보정용)/OOS(20일, 미사용) 분할검증에서 OOS 4개 "
            "지표(거래수/총수익/복리/MDD) 전부 개선 확인. TW2의 선택형 보조필터이며 +1 DOWN_BLUE와 독립적으로 켤 수 있습니다. 기본 OFF."
        ),
    )
with _teg_cols[1]:
    if bool(_teg_on) != bool(getattr(state, "time_window_teg_filter_enabled", False)):
        res = service.set_time_window_teg_filter_enabled(bool(_teg_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"+TEGv2 → {'ON' if _teg_on else 'OFF'}")
            st.rerun()
    else:
        st.caption(
            f"+TEGv2={'ON' if state.time_window_teg_filter_enabled else 'OFF'} · "
            f"오전 진입 {int(getattr(state, 'time_window_morning_entry_count', 0) or 0)}/{macd2_config.MAX_MORNING_ENTRIES} · "
            f"오후 진입 {int(getattr(state, 'time_window_afternoon_entry_count', 0) or 0)}/{macd2_config.MAX_AFTERNOON_ENTRIES} · "
            f"오늘 추가진입 사용={'Y' if getattr(state, 'time_window_teg_count_cap_bypass_used', False) else '-'} · "
            f"포지션관리 활성={'Y' if getattr(state, 'time_window_position_active', False) else '-'}"
            + (f" ({getattr(state, 'time_window_active_mode', '') or ''})" if getattr(state, 'time_window_position_active', False) else "")
        )
        if getattr(state, "last_time_window_teg_candidate_at", None):
            st.caption(
                "최근 TEGv2 후보: "
                f"{'승인' if getattr(state, 'last_time_window_teg_approved', False) else '거절'} · "
                f"사유={', '.join(getattr(state, 'last_time_window_teg_reject_reasons', []) or ['-'])}"
            )

_tw2_cols = st.columns([1.4, 1.6])
with _tw2_cols[0]:
    _tw2_on = st.checkbox(
        "시간대별 최적거래 필터 (TW2)",
        value=bool(getattr(state, "time_window_2_filter_enabled", False)),
        key="macd2_time_window_2_filter_toggle",
        help=(
            "TW1과 완전히 동일한 T+3 재확인/품질점수/시간대/최대진입횟수 게이트+포지션관리 래더를 그대로 쓰되, "
            "진입 시점에 두 가지 veto를 추가로 검사합니다: ① 확정봉 종가가 진입방향 기준 세션 VWAP보다 "
            f"{abs(macd2_config.TW2_VWAP_VETO_THRESHOLD_PCT):.1f}% 이상 불리한 쪽이면 스킵, ② 최근 "
            f"{macd2_config.TW2_RECENT_CROSS_LOOKBACK_MINUTES}분 안에 확정 크로스오버가 "
            f"{macd2_config.TW2_RECENT_CROSS_VETO_COUNT}회 이상이면(휩쏘 구간) 스킵. 통과한 진입은 TP2만 "
            f"+{macd2_config.MORNING_TP2*100:.1f}%→+{macd2_config.TW2_MORNING_TP2*100:.1f}%로 올립니다. "
            "2026-07-10~08-21 연속 29거래일, 앞 19일에서 찾은 임계값을 뒤 10일 OOS에서 재튜닝 없이 재검증 "
            "(복리 +24.5%→+35.7%, MDD 6.8%→5.2%). 기본 ON. +TEGv2와 +1 DOWN_BLUE는 독립 선택형 보조필터입니다."
        ),
    )
with _tw2_cols[1]:
    if bool(_tw2_on) != bool(getattr(state, "time_window_2_filter_enabled", False)):
        res = service.set_time_window_2_filter_enabled(bool(_tw2_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"시간대별 최적거래 필터 (TW2) → {'ON' if _tw2_on else 'OFF'}")
            st.rerun()
    else:
        st.caption(
            f"TW2={'ON' if state.time_window_2_filter_enabled else 'OFF'} · "
            f"오전 진입 {int(getattr(state, 'time_window_morning_entry_count', 0) or 0)}/{macd2_config.MAX_MORNING_ENTRIES} · "
            f"오후 진입 {int(getattr(state, 'time_window_afternoon_entry_count', 0) or 0)}/{macd2_config.MAX_AFTERNOON_ENTRIES} · "
            f"포지션관리 활성={'Y' if getattr(state, 'time_window_position_active', False) else '-'}"
            + (f" ({getattr(state, 'time_window_active_mode', '') or ''})" if getattr(state, 'time_window_position_active', False) else "")
        )

_dbe_cols = st.columns([1.4, 1.6])
with _dbe_cols[0]:
    _dbe_on = st.checkbox(
        "+1 DOWN_BLUE",
        value=bool(getattr(state, "down_blue_exception_filter_enabled", False)),
        key="macd2_down_blue_exception_toggle",
        disabled=not bool(state.time_window_2_filter_enabled),
        help=(
            "TW2가 거절한 DOWN_BLUE 플래그 중, 다른 조건 없이 하루 최대 1회만 추가로 "
            "진입합니다. 56거래일 TRAIN/VAL/OOS 백테스트에서 조건 없이 그대로 허용하는 쪽이 세 구간 모두 일관되게 "
            "개선되어 채택됨(연쇄복리 69.3%→105.3%). 둘 다 꺼져있으면 효과 없음. 기본 OFF."
        ),
    )
with _dbe_cols[1]:
    if bool(_dbe_on) != bool(getattr(state, "down_blue_exception_filter_enabled", False)):
        res = service.set_down_blue_exception_filter_enabled(bool(_dbe_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"+1 DOWN_BLUE → {'ON' if _dbe_on else 'OFF'}")
            st.rerun()
    else:
        st.caption(
            f"+1 DOWN_BLUE={'ON' if state.down_blue_exception_filter_enabled else 'OFF'} · "
            f"오늘 사용={'Y' if getattr(state, 'daily_down_blue_exception_used', False) else '-'}"
        )

_nf_cols = st.columns([1.4, 1.6])
with _nf_cols[0]:
    _nf_on = st.checkbox(
        "무필터 09:00-11:00 즉시청산",
        value=bool(getattr(state, "no_filter_0900_1100_enabled", False)),
        key="macd2_no_filter_0900_1100_toggle",
        help=(
            "품질점수/T+3 대기 없이 09:00-11:00에 확정 플래그가 뜨면 즉시 진입하고, 반대신호가 뜨면 "
            "항상 즉시 매도합니다(휩쏘-내성 유예 없음 — 시간대별 최적거래 필터 전용 로직). "
            "56거래일 TRAIN/VAL/OOS corrected-clock 백테스트에서 TW필터+휩쏘내성보다 우위(56일 복리 "
            "+104.8% vs +15.7%)로 확인되어 추가. 시간대별 최적거래 필터와 동시에 켜지면 그쪽이 우선합니다. 기본 OFF."
        ),
    )
with _nf_cols[1]:
    if bool(_nf_on) != bool(getattr(state, "no_filter_0900_1100_enabled", False)):
        res = service.set_no_filter_0900_1100_filter_enabled(bool(_nf_on), changed_by="ui")
        if res.get("ok"):
            st.caption(f"무필터 09:00-11:00 즉시청산 → {'ON' if _nf_on else 'OFF'}")
            st.rerun()
    else:
        st.caption(
            f"무필터 09:00-11:00 즉시청산={'ON' if state.no_filter_0900_1100_enabled else 'OFF'}"
            + (
                " · TW2/TEG가 우선 적용됩니다"
                if state.no_filter_0900_1100_enabled and (state.time_window_teg_filter_enabled or state.time_window_2_filter_enabled)
                else ""
            )
        )

# Re-read after potential command
snapshot = service.get_snapshot()
state = snapshot["state"]
worker_stats = snapshot["worker"] or {}
quotes = snapshot["quotes"] or {}
bootstrap_last_result = snapshot.get("bootstrap_last_result")
today_signal_overview = snapshot.get("today_signal_overview") or []
trading_date = state.session_date or pd.Timestamp.now().strftime("%Y%m%d")
signal_rows = [r for r in ledger.load_signal_ledger(limit=2000) if r.get("trading_date") == trading_date][-100:]

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
# 2026-08-21 fix: order_block_reason이 POSITION_DATA_ERROR/POSITION_MISMATCH일
# 때 실제 원인(KIS 예외/응답 msg1 등)이 position_reconcile_diag에 이미 저장돼
# 있는데도 화면 어디에도 노출되지 않아, "position data error"라는 코드값만
# 보고는 진짜 원인(레이트리밋인지, 계좌 조회 실패인지)을 서버 로그 없이는 알
# 방법이 없었다 — 진단이 가장 필요한 순간에 이미 있는 정보를 숨기고 있던 셈.
_recon_diag = state.position_reconcile_diag or {}
_recon_reason = _recon_diag.get("mismatch_reason") or _recon_diag.get("broker_response_error")
if _recon_reason:
    st.write(f"포지션 조회 실패 상세: `{_recon_reason}`")

# 2026-08-20 추가: Worker._run_loop이 이미 내부적으로 추적하고 있던
# last_exception/last_tick_age_sec/stalled가 지금까지 UI 어디에도 노출되지
# 않아, "틱이 왜 멈췄는지" 진단할 방법이 대시보드에 전혀 없었다(사용자가
# 직접 코드/서버 로그를 봐야만 알 수 있었음). Worker 상태가 STALLED/DEAD가
# 아니라도(스레드 자체는 is_alive()=True로 살아있는데 내부적으로 멈춰있는
# 경우) age/exception을 그대로 보여줘 다음에 이런 상황이 재발하면 여기서
# 바로 원인을 볼 수 있게 한다.
#
# 2026-08-21 fix: 이 블록 전체가 `if worker_stats:`로 감싸여 있어, 정작
# 진단이 가장 필요한 순간(self._worker가 None인 STALLED/DEAD 상태)에는
# worker_stats가 빈 dict가 되어 화면에서 통째로 사라졌다 — "표시해달라고
# 했는데 없어졌다"는 실제 원인. 항상 렌더링하고, 데이터가 없으면 각 필드가
# 개별적으로 "-"만 보여주도록 변경한다.
last_tick_age = worker_stats.get("last_tick_age_sec")
stalled = bool(worker_stats.get("stalled"))
last_exc = worker_stats.get("last_exception")
last_tick_at_raw = worker_stats.get("last_tick_at")
try:
    last_tick_at_display = datetime.fromisoformat(last_tick_at_raw).astimezone(macd2_config.KST).strftime("%H:%M:%S") if last_tick_at_raw else "-"
except ValueError:
    last_tick_at_display = "-"
quote_fetch_times = [snap.fetched_at for snap in quotes.values() if snap is not None and snap.fetched_at]
last_quote_checked_display = max(quote_fetch_times).astimezone(macd2_config.KST).strftime("%H:%M:%S") if quote_fetch_times else "-"
d1, d2, d3 = st.columns(3)
d1.metric("마지막 tick 시간", last_tick_at_display, delta="STALLED" if stalled else None, delta_color="inverse" if stalled else "normal")
d2.metric("마지막 조회시간", last_quote_checked_display)
d3.metric("누적 tick 수", worker_stats.get("tick_n", "-"))
if last_exc:
    st.error(f"Worker 마지막 예외 (다음 성공 tick까지 유지됨):\n```\n{last_exc}\n```")

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

s1, s2, s3 = st.columns(3)
_flag_bar_time = "-"
_latest_flag_direction = state.latest_primary_flag.value if state.latest_primary_flag else "-"
_latest_flag_signal_id = state.latest_primary_signal_id
_latest_event_rows = list(today_signal_overview) + [row for row in signal_rows if _is_display_signal(row)]
if _latest_event_rows:
    _latest_overview_flag = _latest_flag_event(_latest_event_rows)
    if _latest_overview_flag is not None:
        _latest_flag_direction = _latest_overview_flag.get("direction") or _latest_flag_direction
        _latest_flag_signal_id = _latest_overview_flag.get("signal_id") or _latest_flag_signal_id
        _flag_dt = _parse_flag_event_time(_latest_overview_flag)
        _flag_bar_time = _flag_dt.strftime("%H:%M:%S") if _flag_dt is not None else "-"
if _flag_bar_time == "-" and _latest_flag_signal_id:
    _parts = _latest_flag_signal_id.split("_")
    if len(_parts) >= 2 and len(_parts[1]) == 6:
        _flag_bar_time = f"{_parts[1][:2]}:{_parts[1][2:4]}:{_parts[1][4:]}"
s1.metric(
    "마지막 FLAG EVENT",
    _latest_flag_direction,
    delta=_flag_bar_time if _flag_bar_time != "-" else None,
)
# 현재 MACD STATE(state.primary_relation)는 이벤트가 새로 발생했는지와 무관하게
# 매 확정봉마다 갱신되는 "지금 MACD가 Signal 위/아래 어디에 있는가"이다 — 예를
# 들어 08:45 BLUE 이벤트 이후 09:00에 새 이벤트가 없어도 이 값은 계속 BELOW로
# 남아, 화면에서 "새 이벤트 없음 = 이전 상태 유지"임을 바로 구분할 수 있다.
_state_label = {"BELOW": "BLUE 유지", "ABOVE": "RED 유지", "EQUAL": "-"}.get(state.primary_relation or "", "-")
s2.metric("현재 MACD STATE", _state_label)
if state.position:
    s3.markdown(
        f"""
        <div data-testid="metric-container" style="width:100%;">
          <label style="font-size:14px;color:rgba(49,51,63,.6);">보유 종목</label>
          <div style="font-size:20px;line-height:1.25;font-weight:600;white-space:normal;word-break:keep-all;">
            {escape(str(state.position.symbol))}<br>
            <span style="font-size:70%;">평단 {float(state.position.avg_price or 0):,.0f}</span><br>
            <span style="font-size:70%;">{int(state.position.quantity or 0):,}주 보유</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
else:
    s3.metric("보유 종목", "flat")

exec_rows_all = ledger.load_execution_ledger(limit=2000)
exec_rows = ledger.filter_execution_rows_by_trading_date(exec_rows_all, trading_date)

st.subheader("오늘의 거래 요약")


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


sell_rows_today = [r for r in exec_rows if r.get("side") == "SELL"]
round_trip_count = len(sell_rows_today)
total_gross_pnl = sum(_num(r.get("gross_pnl")) for r in exec_rows)
total_net_pnl = sum(_num(r.get("net_pnl")) for r in exec_rows)
# 세금+수수료+슬리피지를 합친 총비용 = gross와 net의 차이. 각 SELL 레그 자체의
# net_pnl은 이미 매수/매도 수수료+거래세+슬리피지를 전부 반영해 계산되므로
# (app.trading.trading_cost_engine.TradeCostEngine.compute_net_pnl), 원장의
# 개별 fee/slippage 컬럼(매도측 수수료·슬리피지만 따로 담음, 세금/청산비용은
# 컬럼에 없음)을 각각 더하는 대신 이 차이값을 쓰면 항목이 어디에 저장됐는지와
# 무관하게 항상 정확하다 -- reconcile로 발견된 진입처럼 매수 레그 자체가
# 원장에 없는 경우에도 매도(청산) 레그의 net_pnl은 이미 완전한 값이라 영향 없음.
total_cost = total_gross_pnl - total_net_pnl

sum1, sum2, sum3 = st.columns(3)
sum1.metric("오늘 왕복거래 횟수", f"{round_trip_count}건")
sum2.metric("총 수수료+세금+슬리피지", f"{total_cost:,.0f}원")
sum3.metric("총 순수익", f"{total_net_pnl:,.0f}원")

st.subheader("신호 원장 (오늘, 최근 100건)")
if signal_rows:
    timeline_rows = _signal_timeline_rows(signal_rows)
    if timeline_rows:
        st.dataframe(pd.DataFrame(timeline_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("오늘 표시할 플래그/주문 신호가 없습니다.")
    with st.expander("신호 원장 전체 컬럼 보기 (진단용)"):
        st.dataframe(pd.DataFrame(signal_rows), use_container_width=True)
else:
    st.caption("오늘 기록된 신호가 없습니다.")

with st.expander("체결 원장 전체 컬럼 보기 (진단용, 오늘 최근 100건)"):
    if exec_rows:
        st.dataframe(pd.DataFrame(exec_rows[-100:]), use_container_width=True)
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
- **시간대별 최적거래 필터 TW2(기본 ON)**: 이 필터가 진입권한 + 포지션 관리(TP1/TP2/손절 래더)를 모두 담당 — 완성봉 플래그 확정 후 다음 완성 3분봉(T+3)에서 재확인해야만 진입, VWAP 역행 veto/최근30분 교차과다 veto 추가, TP2 5%→6%.
- **+TEGv2(옵션, 기본 OFF)**: 기존 TW2 하루 3회 진입한도를 모두 소진한 뒤 REJECT_MAX_ENTRY_COUNT 때문에만 막힌 후보에 한해 하루 1회 TEGv2 검증 통과 시 추가 진입.
- **+1 DOWN_BLUE(옵션, 기본 OFF)**: ON이면 TW2가 거절한 DOWN_BLUE 플래그 중 하루 최대 1회만 다른 조건 없이 추가로 진입.
- **09:03 예약 매수(옵션)**: 개장 직후 데이터 부족으로 이른 플래그를 놓치는 문제 대응 — 미리 예약해두면 09:03에 지정 방향 ETF를 자동으로 전량매수(하루 1회).
        """
    )
