"""
test_auth_gate.py — Render 로그인 비밀번호 게이트 검증.

streamlit.testing.v1.AppTest로 실제 Streamlit 스크립트 실행을 시뮬레이션한다
(위젯 트리/session_state까지 실제 앱과 동일하게 동작).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

_APP_PATH = str(Path(__file__).parent / "_fixtures" / "auth_gate_app.py")
_PASSWORD = "correct-horse-battery-staple"


def _fresh_app() -> AppTest:
    return AppTest.from_file(_APP_PATH)


def test_shows_login_only_before_authentication(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", _PASSWORD)
    at = _fresh_app()
    at.run()

    assert any("로그인" in t.value for t in at.title)
    assert not any("PROTECTED_CONTENT" in str(m.value) for m in at.markdown)
    assert len(at.text_input) == 1


def test_correct_password_unlocks_protected_content(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", _PASSWORD)
    at = _fresh_app()
    at.run()

    at.text_input[0].input(_PASSWORD).run()
    at.button[0].click().run()

    assert at.session_state["app_auth_authenticated"] is True
    assert any("PROTECTED_CONTENT" in str(m.value) for m in at.markdown)
    assert not at.exception


def test_wrong_password_shows_error_and_stays_locked_out_of_content(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", _PASSWORD)
    at = _fresh_app()
    at.run()

    at.text_input[0].input("wrong-password").run()
    at.button[0].click().run()

    assert "app_auth_authenticated" not in at.session_state or not at.session_state["app_auth_authenticated"]
    assert any("올바르지 않습니다" in e.value for e in at.error)
    assert not any("PROTECTED_CONTENT" in str(m.value) for m in at.markdown)


def test_five_consecutive_failures_lock_session_for_ten_minutes(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", _PASSWORD)
    at = _fresh_app()
    at.run()

    for _ in range(5):
        at.text_input[0].input("wrong-password").run()
        at.button[0].click().run()

    assert any("5회 연속 실패" in e.value for e in at.error)
    assert at.session_state["app_auth_locked_until"] is not None

    # 잠긴 동안에는 비밀번호 입력 폼 자체가 더 이상 노출되지 않는다(정답을 알아도 통과 불가).
    at.run()
    assert len(at.text_input) == 0
    assert any("잠겼습니다" in e.value for e in at.error)


def test_password_input_and_env_value_never_echoed_to_ui(monkeypatch):
    """비밀번호 입력값·환경변수는 로그/UI에 절대 출력 금지."""
    monkeypatch.setenv("APP_PASSWORD", _PASSWORD)
    at = _fresh_app()
    at.run()

    at.text_input[0].input("wrong-but-not-secret-guess").run()
    at.button[0].click().run()

    rendered_text = " ".join(
        str(getattr(el, "value", "")) for el in (list(at.error) + list(at.markdown) + list(at.title) + list(at.caption))
    )
    assert _PASSWORD not in rendered_text
    assert "wrong-but-not-secret-guess" not in rendered_text


def test_fails_closed_when_app_password_not_configured(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    at = _fresh_app()
    at.run()

    assert not any("PROTECTED_CONTENT" in str(m.value) for m in at.markdown)
    assert any("APP_PASSWORD" in e.value for e in at.error)
    # 비밀번호 입력 폼 자체가 없다 — 설정 전에는 그 무엇으로도 통과할 수 없다(fail-closed).
    assert len(at.text_input) == 0


def test_logout_button_appears_after_login_and_clears_authentication(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", _PASSWORD)
    at = _fresh_app()
    at.run()
    at.text_input[0].input(_PASSWORD).run()
    at.button[0].click().run()
    assert at.session_state["app_auth_authenticated"] is True

    logout_buttons = [b for b in at.sidebar.button if "로그아웃" in b.label]
    assert len(logout_buttons) == 1

    # AppTest 하네스 한계 — st.form 안 위젯이 사라진 채로 두 번째 rerun을 하면
    # 그 위젯의 session_state 키를 잃어버려 KeyError가 난다(로그인 폼은 인증 후
    # 렌더링되지 않으므로). 실제 앱 동작과 무관한 하네스 문제이므로, 값을 다시
    # 채워 rerun이 정상적으로 진행되게 한 뒤 로그아웃 버튼의 실제 효과를 검증한다.
    at.session_state["app_login_password_input"] = ""
    logout_buttons[0].click().run()

    assert at.session_state["app_auth_authenticated"] is False
    assert not any("PROTECTED_CONTENT" in str(m.value) for m in at.markdown)
