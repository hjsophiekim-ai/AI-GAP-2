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
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pandas as pd
import streamlit as st

from app.ui.auth_gate import require_login

require_login()

from app.trading.mu_macd import config as mu_config  # noqa: E402
from app.trading.mu_macd import ledger  # noqa: E402
from app.trading.mu_macd.service import get_service  # noqa: E402

st.title("MU MACD 자동매매")
st.caption(
    "완전 독립 신규 모듈 · MACD2/TSLA_AUTO와 상태·원장·잠금 미공유 · "
    "신호소스=마이크론(MU) 장외/주간거래 WebSocket(HDFSCNT0/RBAQMU) · "
    "매매대상=0193T0(레버리지)/0197X0(인버스) — 방향 매핑: MU RED→레버리지, MU BLUE→인버스"
)

service = get_service()
status = service.status()

st.warning(
    "⚠️ 아직 MOCK 검증 단계입니다 — REAL 주문은 비활성화되어 있습니다 "
    "(운영 승인 전까지 mode='real' 시작 버튼 없음).",
    icon="⚠️",
)

col1, col2, col3 = st.columns(3)
col1.metric("Worker 상태", "RUNNING" if status["worker_alive"] else "STOPPED")
col2.metric("모드", status["mode"])
col3.metric("예산", f"{status['budget']:,.0f}원")

st.subheader("제어")
c1, c2 = st.columns(2)
with c1:
    budget = st.number_input("예산(원)", min_value=100_000.0, value=float(mu_config.DEFAULT_BUDGET), step=100_000.0)
    if st.button("MOCK 모드로 자동매매 시작", disabled=status["worker_alive"]):
        result = service.start(mode="mock", budget=budget)
        st.write(result)
        st.rerun()
with c2:
    if st.button("자동매매 중지", disabled=not status["worker_alive"]):
        result = service.stop()
        st.write(result)
        st.rerun()

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

pos = status["position"]
if pos is not None:
    st.info(f"보유 중: {pos.symbol} x{pos.quantity} @ {pos.avg_price:,.1f}")
else:
    st.info("현재 포지션: flat")

if status["order_block_reason"]:
    st.write(f"최근 block/skip 사유: `{status['order_block_reason']}`")

st.subheader("신호 원장 (최근 100건)")
signal_rows = ledger.load_signal_ledger(limit=100)
if signal_rows:
    st.dataframe(pd.DataFrame(signal_rows), use_container_width=True)
else:
    st.caption("아직 기록된 신호가 없습니다.")

st.subheader("체결 원장 (최근 100건)")
exec_rows = ledger.load_execution_ledger(limit=100)
if exec_rows:
    st.dataframe(pd.DataFrame(exec_rows), use_container_width=True)
else:
    st.caption("아직 기록된 체결이 없습니다.")

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
- **신규진입 차단 조건**(청산에는 영향 없음): WS 끊김/stale(>{mu_config.WS_STALE_MAX_SEC}s), 웜업 3분봉 {mu_config.WARMUP_MIN_3M_BARS}개 미달, 09:00 이전/{mu_config.NEW_ENTRY_CUTOFF} 이후.
        """
    )
