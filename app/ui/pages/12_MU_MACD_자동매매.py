"""
12_MU_MACD_자동매매.py — ReadOnly UI for MU_MACD (독립 신규 모듈)

MU_MACD는 app/trading/mu_macd/* 로 macd2/tsla_auto와 완전히 분리되어 있다
(별도 worker/state/ledger/cache/lock — app.trading.mu_macd.config 참조).
신호 소스는 마이크론(MU) 장외/주간거래 WebSocket(HDFSCNT0/RBAQMU)이고, 매매
대상은 macd2와 같은 두 ETF(0193T0/0197X0)이지만 신호 판단은 완전히 별개다.

UI는 command 기록(시작/중지)과 service.status()/ledger 읽기만 수행한다.
"""
from __future__ import annotations

import sys
from datetime import datetime, time as _time
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pandas as pd
import streamlit as st

from app.ui.auth_gate import require_login

require_login()

from app.config import get_config, get_kis_account_config, mask_account  # noqa: E402
from app.trading.mu_macd import config as mu_config  # noqa: E402
from app.trading.mu_macd import ledger  # noqa: E402
from app.trading.mu_macd.service import get_service  # noqa: E402

# 정규거래시간(KRX 09:00-15:30) 밖 데이터는 24시간 MU 시세 수집을 위해 계속
# 쌓이지만(웜업이 끊기면 플래그가 못 나옴), 대시보드에는 "오늘 + 정규거래시간"
# 신호/체결만 보여준다 -- 원장 CSV 자체나 worker 로직은 건드리지 않는다.
_REGULAR_SESSION_CLOSE = _time(15, 30)


def _today_regular_session_rows(rows: list[dict], ts_col: str) -> list[dict]:
    today_str = datetime.now(mu_config.KST).strftime("%Y%m%d")
    filtered = []
    for row in rows:
        raw = row.get(ts_col)
        if not raw:
            continue
        try:
            ts_kst = datetime.fromisoformat(raw).astimezone(mu_config.KST)
        except ValueError:
            continue
        if ts_kst.strftime("%Y%m%d") != today_str:
            continue
        if not (mu_config.SESSION_OPEN <= ts_kst.time() < _REGULAR_SESSION_CLOSE):
            continue
        filtered.append(row)
    return filtered


st.title("MU MACD 자동매매")
st.caption(
    "완전 독립 신규 모듈 · MACD2/TSLA_AUTO와 상태·원장·잠금 미공유 · "
    "신호소스=마이크론(MU) 장외/주간거래 WebSocket(HDFSCNT0/RBAQMU) · "
    "매매대상=0193T0(레버리지)/0197X0(인버스) — 방향 매핑: MU RED→레버리지, MU BLUE→인버스"
)

service = get_service()
status = service.status()
cfg = get_config()

col1, col2, col3 = st.columns(3)
col1.metric("Worker 상태", "RUNNING" if status["worker_alive"] else "STOPPED")
col2.metric("모드", status["mode"])
col3.metric("예산", f"{status['budget']:,.0f}원")

if status.get("flags_only_active"):
    st.warning(
        "⚠️ REAL 주문 인증이 끊긴 상태입니다 — MU 시세 수집과 3분봉 MACD 플래그 감지/기록은 계속 진행 중이지만, "
        "실제 계좌의 주문 실행·보유 포지션 감시(reconcile/손절/퀵프로핏/강제청산)는 전부 멈춰 있습니다. "
        "실제 보유 중인 포지션을 다시 보호하려면 아래에서 확인 문구를 다시 입력하고 \"자동매매 시작\"을 눌러주세요.",
        icon="⚠️",
    )

st.subheader("계좌 / 제어")
c1, c2, c3 = st.columns([1.2, 1.2, 1])
with c1:
    mode = st.radio(
        "계좌 모드", ["mock", "real"], index=0 if status["mode"] != "real" else 1,
        horizontal=True, key="mu_macd_mode",
    )
with c2:
    budget = st.number_input("예산(원)", min_value=100_000.0, value=float(status["budget"] or mu_config.DEFAULT_BUDGET), step=100_000.0)
with c3:
    try:
        acct = get_kis_account_config(mode)
        masked = acct.get("masked_account") or mask_account(acct.get("account_no", ""))
    except Exception:
        masked = None
    st.metric("계좌", masked or "(미설정)")

start_kwargs: dict = {}
if mode == "real":
    st.error("REAL(실전) 모드 — 확인 문구 입력 후에만 시작 가능. 실제 계좌에서 실제 주문이 체결됩니다.")
    expected = str(cfg.real_confirm_text() or "LIVE")
    confirm_in = st.text_input(f"REAL 확인 문구 (정확히 `{expected}` 입력)", type="password", key="mu_macd_real_confirm")
    real_toggle = st.checkbox("REAL 주문 활성화", key="mu_macd_real_toggle")
    start_kwargs = {
        "confirm_text": confirm_in,
        "runtime_enable_real_buy": bool(real_toggle),
        "runtime_enable_real_sell": bool(real_toggle),
    }
else:
    st.info("MOCK 모드 — KIS 모의투자 계좌")

b1, b2 = st.columns(2)
with b1:
    if st.button("자동매매 시작", type="primary", use_container_width=True, disabled=status["worker_alive"]):
        result = service.start(mode=mode, budget=budget, **start_kwargs)
        st.write(result)
        st.rerun()
with b2:
    if st.button("자동매매 중지", use_container_width=True, disabled=not status["worker_alive"]):
        result = service.stop()
        st.write(result)
        st.rerun()

st.caption("수동 진입 (MU MACD 신호 확정 무시, 현재 예산 내 즉시 전량매수 — 이미 보유 중이면 거부)")
m1, m2 = st.columns(2)
with m1:
    if st.button("현재시점 레드(레버리지) 전량매수", use_container_width=True):
        res = service.manual_entry("UP_RED")
        if res.get("ok"):
            st.success(f"레버리지 매수 체결: {res.get('symbol')} {res.get('quantity')}주 @ {res.get('price')}")
        else:
            st.error(f"레버리지 매수 실패: {res.get('message') or res.get('block_reason')}")
        st.rerun()
with m2:
    if st.button("현재시점 블루(인버스) 전량매수", use_container_width=True):
        res = service.manual_entry("DOWN_BLUE")
        if res.get("ok"):
            st.success(f"인버스 매수 체결: {res.get('symbol')} {res.get('quantity')}주 @ {res.get('price')}")
        else:
            st.error(f"인버스 매수 실패: {res.get('message') or res.get('block_reason')}")
        st.rerun()

st.caption("수동 전량청산 (자동매매는 계속 유지, 현재 보유 포지션만 지금 즉시 매도)")
if st.button("현재 보유물량 전량청산", use_container_width=True):
    res = service.manual_exit()
    if res.get("ok"):
        st.success(f"전량청산 체결: {res.get('symbol')} {res.get('quantity')}주 @ {res.get('price')}")
    else:
        st.error(f"전량청산 실패: {res.get('message') or res.get('block_reason')}")
    st.rerun()

_qp_cols = st.columns([1.4, 1.6])
with _qp_cols[0]:
    _qp_on = st.checkbox(
        "퀵 Profit 익절", value=bool(status["quick_profit_enabled"]),
        key="mu_macd_quick_profit_toggle",
        help=f"보유 중 순수익률이 +{mu_config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT}% 도달 시 즉시 전량 익절 (MU 플래그/손절과 독립적인 별도 청산 로직)",
    )
with _qp_cols[1]:
    if bool(_qp_on) != bool(status["quick_profit_enabled"]):
        res = service.set_quick_profit_enabled(bool(_qp_on))
        if res.get("ok"):
            st.caption(f"퀵 Profit 익절 → {'ON' if _qp_on else 'OFF'}")
            st.rerun()
    else:
        st.caption(f"퀵 Profit 익절={'ON' if status['quick_profit_enabled'] else 'OFF'}")

_ep_cols = st.columns([1.4, 1.6])
with _ep_cols[0]:
    _ep_on = st.checkbox(
        "신규진입 일시정지", value=bool(status["entry_paused"]),
        key="mu_macd_entry_paused_toggle",
        help="켜면 MU 시세 수집·3분봉 MACD 플래그 판정·손절/퀵프로핏/강제청산/reconcile은 전부 그대로 동작하고, "
             "새로 진입하는 매수(플랫 진입 및 반대 플래그의 재매수)만 막습니다. 반대 플래그가 뜨면 보유 포지션은 그대로 매도됩니다.",
    )
with _ep_cols[1]:
    if bool(_ep_on) != bool(status["entry_paused"]):
        res = service.set_entry_paused(bool(_ep_on))
        if res.get("ok"):
            st.caption(f"신규진입 → {'일시정지' if _ep_on else '재개'}")
            st.rerun()
    else:
        st.caption(f"신규진입={'일시정지' if status['entry_paused'] else '정상'}")

st.subheader("WebSocket / Warm-up 상태")
w1, w2, w3, w4 = st.columns(4)
w1.metric("WS 연결", "OK" if status["ws_connected"] else "끊김")
w2.metric("마지막 tick 시각", status["ws_last_tick_at"] or "-")
w3.metric("웜업 3분봉 수", f"{status['warmup_bars_3m_count']} / {mu_config.WARMUP_MIN_3M_BARS}")
w4.metric("웜업 완료", "YES" if status["warmup_ready"] else "NO")
if status["ws_last_error"]:
    st.error(f"WS 오류: {status['ws_last_error']}")

st.subheader("현재 신호 / 포지션")
s1, s2, s3 = st.columns(3)
s1.metric("마이크론 현재가", status["last_mu_price"] if status["last_mu_price"] is not None else "-")
s2.metric("마지막 플래그 시각(bar 시작 기준)", status["last_flag_display_time"] or "-")
s3.metric("마지막 플래그 방향", status["last_flag_direction"] or "-")

st.subheader("실시간 ETF 가격")
e1, e2, e3 = st.columns(3)
e1.metric(f"레버리지 {mu_config.LONG_SYMBOL}", f"{status['last_long_etf_price']:,.1f}" if status["last_long_etf_price"] is not None else "-")
e2.metric(f"인버스 {mu_config.INVERSE_SYMBOL}", f"{status['last_inverse_etf_price']:,.1f}" if status["last_inverse_etf_price"] is not None else "-")
e3.metric("조회 시각", status["last_etf_quote_at"] or "-")

pos = status["position"]
if pos is not None:
    st.info(f"보유 중: {pos.symbol} x{pos.quantity} @ {pos.avg_price:,.1f}")
else:
    st.info("현재 포지션: flat")

if status["order_block_reason"]:
    st.write(f"최근 block/skip 사유: `{status['order_block_reason']}`")

st.subheader("신호 원장 (오늘 · 정규거래시간 09:00-15:30, 최근 100건)")
signal_rows = _today_regular_session_rows(ledger.load_signal_ledger(limit=2000), "confirmed_at")[-100:]
if signal_rows:
    st.dataframe(pd.DataFrame(signal_rows), use_container_width=True)
else:
    st.caption("오늘 정규거래시간 중 기록된 신호가 없습니다.")

st.subheader("체결 원장 (오늘 · 정규거래시간 09:00-15:30, 최근 100건)")
exec_rows = _today_regular_session_rows(ledger.load_execution_ledger(limit=2000), "timestamp")[-100:]
if exec_rows:
    st.dataframe(pd.DataFrame(exec_rows), use_container_width=True)
else:
    st.caption("오늘 정규거래시간 중 기록된 체결이 없습니다.")

with st.expander("전략 설명"):
    st.markdown(
        f"""
- **신호**: 마이크론(MU) 3분봉 MACD({mu_config.EMA_FAST},{mu_config.EMA_SLOW},{mu_config.EMA_SIGNAL}) confirmed crossover
  (macd2.signal_engine.evaluate_macd_crossover와 동일한 zero-line crossing 규칙 — KIS 화면과 일치하도록 검증됨).
- **데이터**: KIS WebSocket TR_ID={mu_config.WS_TR_ID}, tr_key={mu_config.WS_TR_KEY} (공식 koreainvestment/open-trading-api 스펙).
  REST 분봉조회는 09:00 이후 주간거래 구간을 못 가져와 사용하지 않음(2026-08-12 자체 검증).
- **방향→매수**: MU RED → 0193T0(레버리지), MU BLUE → 0197X0(인버스).
- **반대 플래그**: 보유 포지션 전량매도 후 반대 ETF 매수(entry_gate 통과 시에만 재매수, 매도는 항상 실행).
- **리스크**: 손절 {mu_config.STOP_LOSS_NET_PCT}%, {mu_config.FORCE_LIQUIDATE_AT} 강제청산 — 매 tick마다 플래그 발생 여부와 무관하게 확인.
- **퀵 Profit 익절(옵션, 기본 OFF)**: ON이면 순수익률이 +{mu_config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT}%에 도달하는 즉시 전량 익절 — MU 플래그와 무관하게 매 tick 확인.
- **신규진입 차단 조건**(청산에는 영향 없음): WS 끊김/stale(>{mu_config.WS_STALE_MAX_SEC}s), 웜업 3분봉 {mu_config.WARMUP_MIN_3M_BARS}개 미달, 09:00 이전/{mu_config.NEW_ENTRY_CUTOFF} 이후, {mu_config.MIDDAY_ENTRY_PAUSE_START}~{mu_config.MIDDAY_ENTRY_PAUSE_END} 점심시간 신규진입 휴식, 사용자가 "신규진입 일시정지"를 켠 경우.
- **점심시간 신규진입 휴식(고정 스케줄)**: {mu_config.MIDDAY_ENTRY_PAUSE_START}~{mu_config.MIDDAY_ENTRY_PAUSE_END}에는 신규 진입(플랫 진입, 반대 플래그의 재매수)만 막힘 — 이 시간에 반대 플래그가 뜨면 보유 포지션은 평소처럼 전량 매도되지만 재매수는 하지 않음. {mu_config.MIDDAY_ENTRY_PAUSE_END} 이후 다시 정상적으로 신규 진입.
- **신규진입 일시정지(옵션, 기본 OFF)**: MU 시세 수집·3분봉 MACD 플래그 판정·신호 원장 기록·손절/퀵프로핏/강제청산/reconcile은 전부 그대로 동작 — 새 매수(플랫 진입, 반대 플래그의 재매수)만 막힘. 자동매매 자체를 끄는 "자동매매 중지"와 달리 데이터 수집/웜업은 끊기지 않음.
- **REAL 모드 재시작 시 안전장치**: 서버 재시작(재배포/idle-sleep) 후에는 REAL 계좌 인증이 자동으로 복구되지 않음(확인 문구 재입력 필요) — MOCK만 자동 복구됨. 다만 MU 시세 수집·플래그 감지는 REAL이어도 인증 없이 계속 동작(위 경고 배너 참고) — 단 이 동안은 reconcile/손절/퀵프로핏/강제청산 등 실제 포지션 보호는 전혀 이뤄지지 않으니, 실전 포지션이 있다면 최대한 빨리 다시 로그인해야 함.
        """
    )
