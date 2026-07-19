"""
auth_gate.py — Render 배포 웹앱 로그인 비밀번호 게이트.

st.session_state에 인증 여부를 저장하고, 인증되기 전까지는 로그인 화면만
렌더링한 뒤 st.stop()으로 이후 코드(본문·사이드바·백그라운드 자동매매 시작·
계좌조회·주문 함수 등)가 전혀 실행되지 않게 한다.

Streamlit의 classic 멀티페이지 구조(app/ui/pages/*.py)는 페이지마다 별도
스크립트로 독립 실행되므로, streamlit_app.py 최상단 한 곳에만 게이트를 걸어서는
나머지 페이지를 보호할 수 없다 — 모든 페이지가 각자 최상단(다른 어떤 로직보다
먼저, import 직후)에서 require_login()을 호출해야 한다.

비밀번호는 코드에 하드코딩하지 않고 환경변수 APP_PASSWORD만 사용하며,
APP_PASSWORD가 설정돼 있지 않으면 fail-closed(본문 접근 차단)로 동작한다.
입력값과 환경변수 값은 어디에도 로그/화면에 출력하지 않는다(hmac.compare_digest로만
비교).
"""

from __future__ import annotations

import hmac
import os
import time

import streamlit as st

_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 600  # 10분

_SESSION_AUTHENTICATED = "app_auth_authenticated"
_SESSION_FAIL_COUNT = "app_auth_fail_count"
_SESSION_LOCKED_UNTIL = "app_auth_locked_until"


def _is_locked() -> tuple[bool, float]:
    locked_until = st.session_state.get(_SESSION_LOCKED_UNTIL)
    if not locked_until:
        return False, 0.0
    remaining = locked_until - time.monotonic()
    if remaining <= 0:
        # 잠금 시간이 지났으면 다음 시도부터 다시 5회를 허용한다.
        st.session_state[_SESSION_LOCKED_UNTIL] = None
        st.session_state[_SESSION_FAIL_COUNT] = 0
        return False, 0.0
    return True, remaining


def _render_login_form(app_password: str) -> None:
    st.title("🔒 로그인")
    st.caption("AI-GAP 자동매매 시스템 — 비밀번호를 입력해야 접근할 수 있습니다.")

    locked, remaining = _is_locked()
    if locked:
        minutes_left = max(1, int(remaining // 60) + 1)
        st.error(f"로그인 5회 연속 실패로 이 세션이 잠겼습니다. 약 {minutes_left}분 후 다시 시도하세요.")
        st.stop()

    with st.form("app_login_form"):
        entered = st.text_input("비밀번호", type="password", key="app_login_password_input")
        submitted = st.form_submit_button("로그인")

    if submitted:
        # entered/app_password는 여기서만 비교에 쓰이고, 로그/화면 어디에도 그대로 출력되지 않는다.
        if hmac.compare_digest(entered or "", app_password):
            st.session_state[_SESSION_AUTHENTICATED] = True
            st.session_state[_SESSION_FAIL_COUNT] = 0
            st.session_state[_SESSION_LOCKED_UNTIL] = None
            st.rerun()
        else:
            fail_count = int(st.session_state.get(_SESSION_FAIL_COUNT, 0)) + 1
            st.session_state[_SESSION_FAIL_COUNT] = fail_count
            if fail_count >= _MAX_ATTEMPTS:
                st.session_state[_SESSION_LOCKED_UNTIL] = time.monotonic() + _LOCKOUT_SECONDS
                st.error("비밀번호 5회 연속 실패 — 이 세션은 10분간 잠깁니다.")
            else:
                st.error(f"비밀번호가 올바르지 않습니다. ({fail_count}/{_MAX_ATTEMPTS}회 실패)")

    st.stop()


def _render_logout_widget() -> None:
    try:
        with st.sidebar:
            if st.button("🔓 로그아웃", key="app_auth_logout_button", use_container_width=True):
                st.session_state[_SESSION_AUTHENTICATED] = False
                st.rerun()
    except Exception:
        # 사이드바 렌더링이 어떤 이유로든 실패해도 인증 자체는 막지 않는다(가용성 우선).
        pass


def require_login() -> None:
    """모든 페이지 스크립트 최상단(다른 어떤 코드보다 먼저)에서 호출해야 한다.

    인증되지 않았으면 로그인 화면만 그리고 st.stop()으로 실행을 중단한다 —
    이 함수 호출 이후에 오는 백그라운드 자동매매 시작/계좌조회/주문 함수 등은
    인증 전에는 절대 실행되지 않는다. 인증된 세션이면 즉시 반환하고(로그아웃
    버튼만 사이드바에 추가로 그린 뒤), 호출부의 나머지 코드가 정상 실행된다.
    """
    if st.session_state.get(_SESSION_AUTHENTICATED):
        _render_logout_widget()
        return

    app_password = os.environ.get("APP_PASSWORD")
    if not app_password:
        st.title("🔒 로그인")
        st.error(
            "APP_PASSWORD 환경변수가 설정되지 않아 fail-closed로 접근을 차단합니다. "
            "Render 환경변수에 APP_PASSWORD를 설정한 뒤 다시 배포하세요."
        )
        st.stop()
        return

    _render_login_form(app_password)
