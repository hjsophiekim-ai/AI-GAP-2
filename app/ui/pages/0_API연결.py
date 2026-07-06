"""
0_API연결.py - KIS API 연결 테스트 및 계좌 확인 페이지
"""
import sys
from pathlib import Path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import os
import streamlit as st

try:
    from app.trading.kis_client import create_kis_client, KISTokenError
    from app.config import get_config, get_kis_account_config
except Exception as e:
    st.error(f"모듈 로드 오류: {e}")
    st.stop()

st.title("KIS API 연결")
st.caption("Mock(모의투자) / Real(실전투자) 계좌 연결 상태를 확인합니다.")

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run_test(mode: str, test_name: str) -> None:
    key_client = f"{mode}_client"
    key_ok = f"{mode}_token_ok"

    if test_name == "token":
        with st.spinner("토큰 발급 중..."):
            # 환경변수 체크 (진단용)
            env_info: dict = {}
            base_url_used = ""
            try:
                acc_cfg = get_kis_account_config(mode)
                env_info = acc_cfg.get("env_checks", {})
                base_url_used = acc_cfg.get("base_url", "")
            except Exception:
                pass

            try:
                client = create_kis_client(mode)
                if client is None:
                    st.error(
                        f"{mode.upper()} KIS 클라이언트 생성 실패 — "
                        f".env의 KIS_{mode.upper()}_APP_KEY / KIS_{mode.upper()}_APP_SECRET "
                        f"환경변수를 확인하세요."
                    )
                    return
                token = client.get_access_token()
                st.session_state[key_client] = client
                st.session_state[key_ok] = True
                expires_str = client._token_expires_at.strftime("%H:%M:%S") if client._token_expires_at else "?"
                st.success(
                    f"토큰 발급 성공  |  길이: {len(token)}자  |  "
                    f"base_url: `{client.base_url}`  |  만료: {expires_str}"
                )
            except KISTokenError as exc:
                st.error(f"토큰 발급 실패 (KIS 오류)")
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**HTTP 상태코드:** `{exc.http_status}`")
                    st.markdown(f"**rt_cd:** `{exc.rt_cd or '(없음)'}`")
                    st.markdown(f"**msg_cd:** `{exc.msg_cd or '(없음)'}`")
                    st.markdown(f"**msg1:** `{exc.msg1 or '(없음)'}`")
                    st.markdown(f"**base_url:** `{exc.base_url_used or base_url_used}`")
                with col2:
                    if env_info:
                        st.markdown("**환경변수 상태:**")
                        for k, v in env_info.items():
                            icon = "✅" if v else "❌"
                            st.markdown(f"{icon} `{k}`")
                st.info(
                    "토큰 발급은 일반적으로 장중 여부와 무관하게 가능해야 합니다.  \n"
                    "실패 시 대부분 app key/secret, base_url, 환경변수 또는 KIS 서버 응답 오류입니다."
                )
            except Exception as exc:
                st.error(f"토큰 발급 실패: {exc}")
                if env_info:
                    with st.expander("환경변수 상태"):
                        for k, v in env_info.items():
                            icon = "✅" if v else "❌"
                            st.markdown(f"{icon} `{k}`")
                st.info(
                    "토큰 발급은 일반적으로 장중 여부와 무관하게 가능해야 합니다.  \n"
                    "실패 시 대부분 app key/secret, base_url, 환경변수 또는 KIS 서버 응답 오류입니다."
                )

    elif test_name == "balance":
        client = st.session_state.get(key_client)
        if client is None:
            st.warning("먼저 토큰을 발급하세요.")
            return
        with st.spinner("계좌 잔고 및 주문가능금액 조회 중..."):
            try:
                bd = client.get_account_cash_breakdown()
                balance = client.get_balance()
                if "error" not in balance:
                    withdrawable = bd.get("withdrawable_amount", balance.get("cash", 0))
                    ord_psbl_cash = bd.get("ord_psbl_cash", 0)      # 현금성 주문가능
                    nrcvb_buy_amt = bd.get("nrcvb_buy_amt", 0)      # 재매수가능금액 (앱 기준)
                    orderable = bd.get("orderable_cash", 0)          # 프로그램 사용 기준
                    settlement_pending = bd.get("settlement_pending_cash", 0)
                    pos_cnt = len(balance.get("positions", []))

                    # 종목별 매수가능금액 조회
                    try:
                        stock_buyable = client.get_stock_buyable_amount("005930", 0)
                    except Exception:
                        stock_buyable = orderable

                    col1, col2, col3 = st.columns(3)
                    col1.metric(
                        "인출가능금액 (출금 가능)",
                        f"{withdrawable:,.0f}원",
                        help="dnca_tot_amt — 실제로 출금 가능한 금액. 매수 기준이 아님."
                    )
                    col2.metric(
                        "재매수가능금액 (앱 기준)",
                        f"{nrcvb_buy_amt:,.0f}원",
                        help="nrcvb_buy_amt — 앱의 '주문가능금액'과 일치하는 후보 필드. "
                             "D+2 결제 전 매도대금 포함."
                    )
                    col3.metric(
                        "AI-GAP 매수 기준 금액",
                        f"{orderable:,.0f}원",
                        help="max(nrcvb_buy_amt, ord_psbl_cash) — 프로그램이 매수 한도로 사용하는 값."
                    )
                    col4, col5 = st.columns(2)
                    col4.metric(
                        "순수 현금성 주문가능",
                        f"{ord_psbl_cash:,.0f}원",
                        help="ord_psbl_cash — 현금성 주문가능금액. D+2 매도대금 미포함."
                    )
                    col5.metric(
                        "종목별 매수가능 (005930 기준)",
                        f"{stock_buyable:,.0f}원",
                        help="inquire-psbl-order API 기준 삼성전자(005930) 매수가능금액."
                    )

                    if settlement_pending > 0:
                        st.info(
                            f"D+2 미결제 추정 매도대금: **{settlement_pending:,.0f}원** — "
                            "매수는 가능하나 인출(출금)은 불가합니다."
                        )

                    st.caption(
                        "인출가능금액은 출금 가능 금액입니다. 금요일 매도 등 결제 전 매도대금은 "
                        "인출가능금액에는 반영되지 않지만 국내주식 재매수 가능금액에 포함될 수 있습니다. "
                        "AI-GAP은 매수 판단 시 인출가능금액이 아니라 종목별 매수가능금액을 사용합니다."
                    )
                    st.info(f"보유종목: **{pos_cnt}개**")
                    st.session_state[f"{mode}_balance"] = balance
                    st.session_state[f"{mode}_breakdown"] = bd
                else:
                    err = balance.get("error", "")
                    st.error(f"잔고 조회 실패: {err}")
                    if "HTTP 500" in err or "500" in err:
                        st.warning(
                            "**KIS 모의투자 서버 500 오류 주요 원인:**\n\n"
                            "1. **장 외 시간** — KIS VTS 잔고 조회는 09:00-16:00 KST 범위에서만 정상 작동합니다.\n"
                            "2. **모의투자 권한 미등록** — KIS 개발자 포털에서 해당 App Key가 "
                            "`모의투자(VTS)`용으로 별도 등록되어 있어야 합니다. "
                            "실전 App Key로는 VTS 잔고 조회가 500을 반환할 수 있습니다.\n"
                            "3. **토큰 세션 불일치** — 토큰을 재발급 후 다시 시도해 보세요.\n\n"
                            "KIS 개발자 포털 → '모의투자' 탭에서 App Key 등록 여부를 확인하세요."
                        )
                    elif "40310000" in err or "계좌" in err:
                        st.info("계좌번호를 확인하세요. KIS API는 CANO=8자리 숫자만 허용합니다. "
                                "예: 12345678 (하이픈/상품코드 제외)")
            except Exception as exc:
                st.error(f"잔고 조회 실패: {exc}")

    elif test_name == "positions":
        client = st.session_state.get(key_client)
        if client is None:
            st.warning("먼저 토큰을 발급하세요.")
            return
        with st.spinner("보유종목 조회 중..."):
            try:
                balance = client.get_balance()
                if "error" not in balance:
                    positions = balance.get("positions", [])
                    if positions:
                        st.success(f"보유종목 **{len(positions)}개**")
                        import pandas as pd
                        rows = [
                            {
                                "종목코드": p.get("symbol", ""),
                                "종목명": p.get("name", ""),
                                "보유수량": p.get("quantity", 0),
                                "평균단가": f"{p.get('avg_price', 0):,.0f}",
                                "현재가": f"{p.get('current_price', 0):,.0f}",
                            }
                            for p in positions
                        ]
                        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                    else:
                        st.info("보유 종목 없음")
                else:
                    st.warning(f"조회 실패: {balance.get('error')}")
            except Exception as exc:
                st.warning(f"보유종목 조회 실패: {exc}")

    elif test_name == "buyable":
        client = st.session_state.get(key_client)
        if client is None:
            st.warning("먼저 토큰을 발급하세요.")
            return
        with st.spinner("주문가능금액 조회 중..."):
            try:
                buyable = client.get_buyable_cash()
                if buyable > 0:
                    st.success(f"주문가능금액: **{buyable:,.0f}원**")
                else:
                    st.warning("주문가능금액: 0원 (장외시간에는 조회가 제한될 수 있습니다)")
                st.session_state[f"{mode}_buyable"] = buyable
            except Exception as exc:
                st.warning(f"주문가능금액 조회 실패 (장외시간 제한 가능): {exc}")


# ---------------------------------------------------------------------------
# tabs
# ---------------------------------------------------------------------------

tab_mock, tab_real = st.tabs(["모의투자 (Mock)", "실전투자 (Real)"])

# ── Mock ──────────────────────────────────────────────────────────────────
with tab_mock:
    st.subheader("Mock 계좌 연결 테스트")
    st.info("KIS 모의투자 계좌 (`openapivts.koreainvestment.com:29443`)에 연결합니다.")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        if st.button("① 토큰 발급", key="mock_token_btn", use_container_width=True, type="primary"):
            _run_test("mock", "token")
    with c2:
        if st.button("② 계좌 잔고", key="mock_balance_btn", use_container_width=True):
            _run_test("mock", "balance")
    with c3:
        if st.button("③ 보유종목", key="mock_positions_btn", use_container_width=True):
            _run_test("mock", "positions")
    with c4:
        if st.button("④ 주문가능금액", key="mock_buyable_btn", use_container_width=True):
            _run_test("mock", "buyable")

    st.divider()

    mock_ok = st.session_state.get("mock_token_ok", False)
    mock_buyable = st.session_state.get("mock_buyable")
    mock_balance = st.session_state.get("mock_balance")

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        if mock_ok:
            st.success("토큰: 발급됨")
        else:
            st.error("토큰: 미발급")
    with col_b:
        if mock_balance and "error" not in mock_balance:
            st.success(f"예수금: {mock_balance.get('cash', 0):,.0f}원")
        else:
            st.warning("예수금: 미조회")
    with col_c:
        if mock_buyable is not None:
            st.success(f"주문가능: {mock_buyable:,.0f}원")
        else:
            st.warning("주문가능금액: 미조회")

    if mock_ok and mock_buyable is not None and mock_buyable > 0:
        st.success("Mock 계좌 주문 가능 상태입니다.")
    elif mock_ok:
        st.warning("토큰은 발급됐지만 주문가능금액을 확인하세요.")

# ── Real ──────────────────────────────────────────────────────────────────
with tab_real:
    st.subheader("Real 계좌 연결 테스트")
    st.error("실전투자 계좌 조회만 테스트합니다. **이 페이지에서 주문은 실행되지 않습니다.**")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        if st.button("① 토큰 발급", key="real_token_btn", use_container_width=True, type="primary"):
            _run_test("real", "token")
    with c2:
        if st.button("② 계좌 잔고", key="real_balance_btn", use_container_width=True):
            _run_test("real", "balance")
    with c3:
        if st.button("③ 보유종목", key="real_positions_btn", use_container_width=True):
            _run_test("real", "positions")
    with c4:
        if st.button("④ 주문가능금액", key="real_buyable_btn", use_container_width=True):
            _run_test("real", "buyable")

    st.divider()

    real_ok = st.session_state.get("real_token_ok", False)
    real_buyable = st.session_state.get("real_buyable")
    real_balance = st.session_state.get("real_balance")
    real_breakdown = st.session_state.get("real_breakdown", {})

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        if real_ok:
            st.success("토큰: 발급됨")
        else:
            st.error("토큰: 미발급")
    with col_b:
        withdrawable = real_breakdown.get("withdrawable_amount", real_balance.get("cash", 0) if real_balance else 0)
        if real_balance and "error" not in real_balance:
            st.success(f"인출가능: {withdrawable:,.0f}원")
        else:
            st.warning("인출가능금액: 미조회")
    with col_c:
        nrcvb = real_breakdown.get("nrcvb_buy_amt", 0)
        orderable = real_breakdown.get("orderable_cash", real_buyable or 0)
        if real_breakdown:
            st.success(f"AI-GAP 매수기준: {orderable:,.0f}원")
        elif real_buyable is not None:
            st.success(f"주문가능: {real_buyable:,.0f}원")
        else:
            st.warning("주문가능금액: 미조회")

    if real_ok and orderable > 0:
        st.success("Real 계좌 주문 가능 상태입니다.")
    elif real_ok:
        st.warning("토큰은 발급됐지만 주문가능금액을 확인하세요.")

# ---------------------------------------------------------------------------
# 실전모드 활성화 섹션
# ---------------------------------------------------------------------------
st.divider()
st.subheader("실전모드 활성화")

_real_mode_active = st.session_state.get("real_mode_enabled", False)

if _real_mode_active:
    st.error(
        "현재 실전모드가 활성화되어 있습니다. 실제 계좌에서 매수와 매도가 모두 실행될 수 있습니다.",
        icon="🔴",
    )
    col_deact, _ = st.columns([1, 3])
    with col_deact:
        if st.button("실전모드 해제", type="primary", use_container_width=True):
            st.session_state.pop("real_mode_enabled", None)
            st.session_state.pop("enable_real_buy", None)
            st.session_state.pop("enable_real_sell", None)
            st.success("실전모드가 해제되었습니다. 이제 모의/안전 모드입니다.")
            st.rerun()
else:
    st.success("현재 모의/안전 모드입니다. 실제 주문은 실행되지 않습니다.", icon="🟢")
    st.info(
        "실전모드를 활성화하면 KIS 실전투자 계좌에 실제 주문이 실행됩니다.  \n"
        "활성화 전 다음 조건이 모두 충족되어야 합니다:  \n"
        "1. KIS 실전계좌 환경변수(.env)가 설정되어 있어야 합니다.  \n"
        "2. 아래 확인 문구를 정확히 입력해야 합니다."
    )

    # 환경변수 존재 여부 사전 확인 (값은 절대 출력하지 않음)
    try:
        _cfg_tmp = get_config()
        kis_real = _cfg_tmp._raw.get("kis", {}).get("real", {})
        _env_keys = {
            "APP_KEY":       kis_real.get("app_key_env", "KIS_REAL_APP_KEY"),
            "APP_SECRET":    kis_real.get("app_secret_env", "KIS_REAL_APP_SECRET"),
            "ACCOUNT_NO":    kis_real.get("account_no_env", "KIS_ACCOUNT_NO"),
            "PRODUCT_CODE":  kis_real.get("account_product_code_env", "KIS_ACCOUNT_PRODUCT_CODE"),
        }
        _expected_confirm = _cfg_tmp.real_confirm_text()
    except Exception:
        _env_keys = {
            "APP_KEY": "KIS_REAL_APP_KEY",
            "APP_SECRET": "KIS_REAL_APP_SECRET",
            "ACCOUNT_NO": "KIS_ACCOUNT_NO",
            "PRODUCT_CODE": "KIS_ACCOUNT_PRODUCT_CODE",
        }
        _expected_confirm = "REAL_ORDER_CONFIRMED"

    with st.expander("환경변수 사전 확인", expanded=True):
        all_env_ok = True
        for label, env_name in _env_keys.items():
            exists = bool(os.getenv(env_name, ""))
            icon = "✅" if exists else "❌"
            st.markdown(f"{icon} `{env_name}` — {'설정됨' if exists else '**미설정 (필수)**'}")
            if not exists:
                all_env_ok = False
        if not all_env_ok:
            st.warning("위 환경변수를 .env 파일에 설정한 후 앱을 재시작하세요.")

    st.markdown("")
    act_confirm = st.text_input(
        f"확인 문구 입력 (정확히 `{_expected_confirm}` 를 입력하세요)",
        type="password",
        placeholder=_expected_confirm,
        key="real_mode_confirm_input",
    )

    col_act, _ = st.columns([1, 3])
    with col_act:
        _btn_disabled = not (all_env_ok and act_confirm == _expected_confirm)
        if st.button(
            "실전모드 활성화",
            type="primary",
            use_container_width=True,
            disabled=_btn_disabled,
        ):
            try:
                with st.spinner("실전 계좌 연결 확인 중..."):
                    _test_client = create_kis_client("real")
                if _test_client is None:
                    st.error(
                        "실전 계좌 연결 실패: KIS 클라이언트를 초기화할 수 없습니다.  \n"
                        ".env 파일의 실전계좌 환경변수를 확인하세요."
                    )
                else:
                    st.session_state["real_mode_enabled"] = True
                    st.session_state["enable_real_buy"] = True
                    st.session_state["enable_real_sell"] = True
                    st.success("실전모드가 활성화되었습니다! 이제 실제 계좌로 주문이 실행됩니다.")
                    st.rerun()
            except Exception as exc:
                st.error(f"실전모드 활성화 실패: {exc}")

    if act_confirm and act_confirm != _expected_confirm:
        st.error("확인 문구가 틀립니다. 정확히 입력하세요.")
