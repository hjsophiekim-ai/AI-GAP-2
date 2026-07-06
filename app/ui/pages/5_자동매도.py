"""
5_자동매도.py — 자동매도 감시 및 실행 UI

Mock 계좌: 모의투자 API로 자동매도 테스트.
Real 계좌: 실전모드 활성화 시 실계좌 자동매도.
"""
import sys
from pathlib import Path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import io
import pandas as pd
import streamlit as st

try:
    from app.services.auto_sell_service import AutoSellService
    from app.config import get_config
    from app.trading.kis_mock_broker import KisMockBroker
    from app.trading.kis_real_broker import KisRealBroker
except Exception as e:
    st.error(f"모듈 로드 오류: {e}")
    st.stop()

# ---------------------------------------------------------------------------
# autorefresh (optional)
# ---------------------------------------------------------------------------
_AUTOREFRESH_AVAILABLE = False
try:
    from streamlit_autorefresh import st_autorefresh
    _AUTOREFRESH_AVAILABLE = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get_cfg():
    try:
        return get_config()
    except Exception:
        return None


def _available_accounts() -> list[str]:
    """발급된 토큰이 있는 계좌 목록 반환."""
    accounts = []
    if st.session_state.get("mock_token_ok") and st.session_state.get("mock_client"):
        accounts.append("mock")
    if st.session_state.get("real_token_ok") and st.session_state.get("real_client"):
        accounts.append("real")
    return accounts


def _get_or_create_service(account_type: str) -> "AutoSellService | None":
    """account_type('mock'|'real')에 맞는 AutoSellService 반환."""
    svc_key = f"auto_sell_service_{account_type}"
    if svc_key in st.session_state:
        svc = st.session_state[svc_key]
        if isinstance(svc, AutoSellService):
            return svc

    client = st.session_state.get(f"{account_type}_client")
    if client is None:
        return None

    try:
        cfg = get_config()
        if account_type == "mock":
            broker = KisMockBroker(client)
        else:
            confirm = cfg.real_confirm_text()
            broker = KisRealBroker(
                client, cfg=cfg,
                confirm_text=confirm,
                runtime_real_mode=True,
            )
        svc = AutoSellService(kis_client=client, broker=broker, cfg=cfg)
        st.session_state[svc_key] = svc
        return svc
    except Exception as e:
        st.error(f"AutoSellService 초기화 실패: {e}")
        return None


def _colour_rate(rate: float, stop_loss_rate: float = -2.0) -> str:
    rate_str = f"{rate:+.2f}%"
    if rate >= 5.0:
        return f"🔴 {rate_str}"
    if rate >= 3.0:
        return f"🟡 {rate_str}"
    if rate > 0:
        return f"🟢 {rate_str}"
    if rate <= stop_loss_rate:
        return f"🚨 {rate_str}"
    return f"⚪ {rate_str}"


def _sell_type_label(sell_type: str) -> str:
    return {"half": "절반(+3%)", "full": "전량(+5%)", "stop_loss": "손절(-2%)"}.get(sell_type, sell_type)


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

st.title("자동매도 감시")

cfg = _get_cfg()
real_mode_active: bool = st.session_state.get("real_mode_enabled", False)
auto_sell_on: bool = st.session_state.get("auto_sell_enabled", False)
available = _available_accounts()

# ── 계좌 선택 ──────────────────────────────────────────────────────────────
st.subheader("계좌 선택")

if not available:
    st.warning("발급된 토큰이 없습니다. API연결 페이지에서 Mock 또는 Real 계좌 토큰을 먼저 발급하세요.")
    st.stop()

account_labels = {"mock": "Mock (모의투자)", "real": "Real (실전투자)"}
radio_options = [account_labels[a] for a in available]
prev_account = st.session_state.get("auto_sell_account", available[0])
if prev_account not in available:
    prev_account = available[0]

selected_label = st.radio(
    "사용할 계좌",
    radio_options,
    index=available.index(prev_account),
    horizontal=True,
)
selected_account = available[radio_options.index(selected_label)]

if selected_account != st.session_state.get("auto_sell_account"):
    st.session_state["auto_sell_account"] = selected_account
    st.session_state.pop(f"auto_sell_service_{selected_account}", None)
    st.session_state["auto_sell_enabled"] = False
    st.rerun()

# ── 상단 안전 배너 ─────────────────────────────────────────────────────────
if selected_account == "real":
    if not real_mode_active:
        st.warning("Real 계좌 자동매도를 사용하려면 API연결 페이지에서 실전모드를 먼저 활성화하세요.")
    if auto_sell_on and real_mode_active:
        st.error("자동매도 ON (실계좌): 조건 충족 시 실제 계좌에서 매도 주문이 자동 실행됩니다.", icon="🔴")
    else:
        st.info("자동매도 OFF: 조건을 충족해도 자동 매도되지 않습니다.", icon="ℹ️")
else:
    if auto_sell_on:
        st.warning("자동매도 ON (모의계좌): 모의투자 API로 매도 주문이 자동 실행됩니다.", icon="⚠️")
    else:
        st.info("자동매도 OFF (모의계좌): 조건을 충족해도 자동 매도되지 않습니다.", icon="ℹ️")

# ── 자동매도 토글 ──────────────────────────────────────────────────────────
st.subheader("자동매도 설정")

col_toggle, col_interval = st.columns([2, 2])
with col_toggle:
    toggle_disabled = (selected_account == "real" and not real_mode_active)
    new_auto_sell = st.toggle(
        "자동매도 활성화",
        value=auto_sell_on,
        disabled=toggle_disabled,
        help="Real 계좌는 실전모드 활성화 후 사용 가능합니다." if toggle_disabled else "",
        key="auto_sell_toggle",
    )
    if new_auto_sell != auto_sell_on:
        st.session_state["auto_sell_enabled"] = new_auto_sell
        if not new_auto_sell:
            st.session_state.pop(f"auto_sell_service_{selected_account}", None)
        st.rerun()

interval_seconds = 10
if cfg:
    interval_seconds = cfg._raw.get("auto_sell", {}).get("check_interval_seconds", 10)

with col_interval:
    st.metric("점검 주기", f"{interval_seconds}초")

# ── 자동갱신 및 서비스 실행 ────────────────────────────────────────────────
can_run = auto_sell_on and (selected_account == "mock" or real_mode_active)

if can_run:
    if _AUTOREFRESH_AVAILABLE:
        st_autorefresh(interval=interval_seconds * 1000, key="auto_sell_autorefresh")
    else:
        st.caption("※ streamlit-autorefresh 미설치 — 수동 새로고침 필요")

    svc = _get_or_create_service(selected_account)
    if svc is None:
        st.error(
            f"AutoSellService를 초기화할 수 없습니다. "
            f"API연결 페이지에서 {selected_label} 토큰을 먼저 발급하세요."
        )
    else:
        with st.spinner("가격 점검 중..."):
            try:
                new_results = svc.run_once_if_due(interval_seconds)
                if new_results:
                    prev = st.session_state.get("auto_sell_last_results", [])
                    st.session_state["auto_sell_last_results"] = new_results + prev
            except Exception as e:
                st.error(f"run_once 오류: {e}")

# ── 감시 현황 ──────────────────────────────────────────────────────────────
st.divider()
st.subheader("보유종목 감시 현황")

svc_display = st.session_state.get(f"auto_sell_service_{selected_account}")

if svc_display is None:
    st.info("자동매도를 활성화하면 보유종목 감시 현황이 표시됩니다.")
else:
    last_run = svc_display._last_run_time
    st.caption(
        f"마지막 가격 확인: {last_run.strftime('%H:%M:%S') if last_run else '미실행'}"
    )

    state = svc_display.state
    stop_loss_rate = svc_display._stop_loss_rate
    if not state:
        st.info("감시 중인 보유종목이 없습니다.")
    else:
        rows = []
        for sym, s in state.items():
            sl_done = s.get("stop_loss_executed", False)
            rows.append({
                "종목코드": sym,
                "종목명": s.get("name", ""),
                "매수가": f"{s.get('avg_buy_price', 0):,.0f}원",
                "수익률": _colour_rate(s.get("last_profit_rate", 0.0), stop_loss_rate),
                "절반매도(+3%)": "✅ 완료" if s.get("half_sold") else ("⏳ 대기" if not s.get("all_sold") else "—"),
                "전량매도(+5%)": "✅ 완료" if (s.get("all_sold") and not sl_done) else ("⏳ 대기" if not s.get("all_sold") else "—"),
                f"손절({stop_loss_rate:.1f}%)": "🚨 실행" if sl_done else "⏳ 대기",
                "마지막확인": (s.get("last_checked_at") or "—")[:19].replace("T", " "),
                "오류": s.get("last_error") or "",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ── 마지막 자동매도 결과 ────────────────────────────────────────────────────
last_results = st.session_state.get("auto_sell_last_results", [])
if last_results:
    st.divider()
    st.subheader("마지막 자동매도 실행 결과")
    result_rows = []
    for r in last_results[:20]:
        result_rows.append({
            "시간": r.get("timestamp", ""),
            "종목코드": r.get("symbol", ""),
            "종목명": r.get("name", ""),
            "매도유형": _sell_type_label(r.get("sell_type", "")),
            "수량": r.get("sell_quantity", 0),
            "수익률": f"{r.get('profit_rate', 0.0):+.2f}%",
            "결과": r.get("order_result", ""),
            "주문ID": r.get("order_id", ""),
            "오류": r.get("error_message", ""),
        })
    st.dataframe(pd.DataFrame(result_rows), use_container_width=True, hide_index=True)

# ── 로그 다운로드 ──────────────────────────────────────────────────────────
st.divider()
st.subheader("자동매도 로그")

if svc_display:
    log_rows = svc_display.read_log()
    if log_rows:
        df_log = pd.DataFrame(log_rows)
        st.dataframe(df_log, use_container_width=True, hide_index=True)
        csv_bytes = df_log.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label="CSV 다운로드",
            data=csv_bytes,
            file_name=f"auto_sell_log_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        st.info("아직 자동매도 로그가 없습니다.")
else:
    st.info("자동매도 서비스를 활성화하면 로그가 기록됩니다.")

# ── 설정 요약 ──────────────────────────────────────────────────────────────
st.divider()
with st.expander("자동매도 설정 요약", expanded=False):
    if cfg:
        auto_cfg = cfg._raw.get("auto_sell", {})
        col1, col2 = st.columns(2)
        with col1:
            st.markdown(f"- 절반매도 기준: **+{auto_cfg.get('first_take_profit_rate', 3.0)}%**")
            st.markdown(f"- 절반매도 비율: **{auto_cfg.get('first_take_profit_sell_ratio', 0.5)*100:.0f}%**")
            st.markdown(f"- 전량매도 기준: **+{auto_cfg.get('final_take_profit_rate', 5.0)}%**")
            st.markdown(f"- 손절 기준: **{auto_cfg.get('stop_loss_rate', -2.0):.1f}%** (이하 전량매도)")
        with col2:
            st.markdown(f"- 주문 유형: **{auto_cfg.get('order_type', 'market')}**")
            st.markdown(f"- 장 시작: **{auto_cfg.get('market_start', '09:00')}**")
            st.markdown(f"- 장 종료: **{auto_cfg.get('market_end', '15:20')}**")
        if svc_display:
            st.markdown(f"- state 파일: `{svc_display.state_file_path}`")
            st.markdown(f"- 로그 파일: `{svc_display.log_file_path}`")
