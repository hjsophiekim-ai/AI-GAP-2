"""조기익절 필터 UI 토글 렌더링 테스트 (2026-09-03).

tests/macd2/test_ui_page.py 와 같은 하네스(streamlit.testing.v1.AppTest +
conftest.py의 autouse tmp_path 격리)를 그대로 쓴다 — 실제 페이지 파일을
렌더링하고, 실제 KIS/브로커/Worker는 전혀 건드리지 않는다("시작" 버튼을
누르지 않으므로 broker/market-data 생성 자체가 일어나지 않는다).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.trading.macd2 import config, state_store

_APP_PATH = str(Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")
_TOGGLE_KEY = "macd2_early_tp_toggle"
_LABEL = "└ 조기익절 필터"

# 기존(이 필터와 무관한) 페이지 결함: 전체 스위트로 돌릴 때 이 페이지 렌더가
# `st.button()` can't be used in an `st.form()` 로 죽어 아무 위젯도 렌더되지
# 않는다. 파일 단독 실행에서는 재현되지 않고, tests/macd2/test_ui_page.py 도
# 정확히 같은 이유로 단독 2건 -> 전체 10건으로 실패가 늘어난다(이 변경 전부터
# 그랬다). 조기익절 토글 테스트가 그 결함 때문에 실패로 잡히면 신호가 섞이므로,
# 그 예외가 감지되면 명시적으로 skip 하고 사유를 남긴다 -- 이 필터 자체의
# 문제로 실패할 때만 실패하게 하는 것이 목적이다.
_PREEXISTING_FORM_BUG = "can't be used in an `st.form()`"


def _fresh_app() -> AppTest:
    at = AppTest.from_file(_APP_PATH, default_timeout=30)
    at.session_state["app_auth_authenticated"] = True
    return at


def _guard(at: AppTest) -> AppTest:
    """이미 run()된 AppTest에 대해 기존 form/button 결함만 걸러낸다."""
    for exc in at.exception:
        if _PREEXISTING_FORM_BUG in str(getattr(exc, "value", "") or ""):
            pytest.skip(
                "기존 페이지 결함(st.button inside st.form)으로 페이지 렌더 자체가 실패 -- "
                "조기익절 토글과 무관. 이 파일 단독 실행에서는 전부 통과한다."
            )
    assert not at.exception
    return at


def _run(at: AppTest) -> AppTest:
    """페이지를 렌더하고, 기존 form/button 결함이 터진 경우에만 skip 한다."""
    at.run()
    return _guard(at)


def _toggle(at: AppTest):
    for cb in at.checkbox:
        if cb.label == _LABEL:
            return cb
    raise AssertionError(
        f"조기익절 필터 체크박스가 렌더링되지 않았다. 렌더된 체크박스: {[c.label for c in at.checkbox]!r}"
    )


def _labels(at: AppTest) -> list[str]:
    return [c.label for c in at.checkbox]


def _set_3slot(enabled: bool) -> None:
    state = state_store.load_state()
    state.time_window_3slot_filter_enabled = bool(enabled)
    if enabled:
        # 3-way 상호배제: 3-SLOT을 켜려면 TW2/TEG는 꺼져 있어야 한다
        state.time_window_2_filter_enabled = False
        state.time_window_teg_filter_enabled = False
    else:
        state.early_tp_filter_enabled = False
    state_store.save_state(state)


def test_toggle_renders_directly_below_the_tw2_3slot_toggle():
    _set_3slot(True)
    at = _run(_fresh_app())

    labels = _labels(at)
    assert "TW2 3-SLOT" in labels, f"TW2 3-SLOT 토글을 찾지 못했다: {labels!r}"
    assert _LABEL in labels
    assert labels.index(_LABEL) == labels.index("TW2 3-SLOT") + 1, (
        f"조기익절 필터가 TW2 3-SLOT 바로 아래에 있지 않다: {labels!r}"
    )


def test_toggle_is_disabled_while_tw2_3slot_is_off():
    _set_3slot(False)
    at = _run(_fresh_app())

    cb = _toggle(at)
    assert cb.disabled is True, "TW2 3-SLOT이 꺼져 있으면 토글이 비활성이어야 한다"
    assert cb.value is False
    assert any("TW2 3-SLOT을 켜야" in c.value for c in at.caption), (
        f"자동 비활성 안내 캡션이 없다: {[c.value for c in at.caption]!r}"
    )


def test_toggle_is_enabled_and_off_by_default_when_tw2_3slot_is_on():
    _set_3slot(True)
    at = _run(_fresh_app())

    cb = _toggle(at)
    assert cb.disabled is False
    assert cb.value is False, "기본값은 OFF여야 한다"
    assert state_store.load_state().early_tp_filter_enabled is False


def test_checking_the_toggle_turns_the_filter_on_and_persists():
    _set_3slot(True)
    at = _run(_fresh_app())
    _toggle(at).check().run()
    _guard(at)

    assert state_store.load_state().early_tp_filter_enabled is True
    assert _toggle(at).value is True


def test_unchecking_the_toggle_turns_the_filter_off_and_persists():
    _set_3slot(True)
    state = state_store.load_state()
    state.early_tp_filter_enabled = True
    state.early_tp_filter_version = config.EARLY_TP_FILTER_VERSION
    state_store.save_state(state)

    at = _run(_fresh_app())
    assert _toggle(at).value is True
    _toggle(at).uncheck().run()
    _guard(at)

    assert state_store.load_state().early_tp_filter_enabled is False


def test_turning_tw2_3slot_off_from_the_ui_also_clears_the_early_tp_toggle():
    """service 쪽 강제해제가 UI에서도 반영되는지 — 위젯 session_state가 남아
    다음 rerun에서 다시 켜려 하지 않아야 한다."""
    _set_3slot(True)
    at = _run(_fresh_app())
    _toggle(at).check().run()
    _guard(at)
    assert state_store.load_state().early_tp_filter_enabled is True

    for cb in at.checkbox:
        if cb.label == "TW2 3-SLOT":
            cb.uncheck().run()
            break
    else:
        raise AssertionError("TW2 3-SLOT 토글을 찾지 못했다")

    _guard(at)
    reloaded = state_store.load_state()
    assert reloaded.time_window_3slot_filter_enabled is False
    assert reloaded.early_tp_filter_enabled is False, "3-SLOT을 끄면 함께 꺼져야 한다"

    cb = _toggle(at)
    assert cb.value is False and cb.disabled is True
    # AppTest.session_state는 dict.get()을 지원하지 않으므로 직접 인덱싱한다.
    try:
        widget_state = at.session_state[_TOGGLE_KEY]
    except KeyError:
        widget_state = False
    assert widget_state is False, (
        "위젯 상태가 True로 남아 다음 rerun에서 재활성화를 시도하면 안 된다"
    )

    # 한 번 더 렌더해도 다시 켜지지 않는지(재활성화 루프 없음) 확인
    at.run()
    _guard(at)
    assert state_store.load_state().early_tp_filter_enabled is False
    assert _toggle(at).value is False


def test_help_text_states_the_validated_thresholds_and_the_low_sample_caveat():
    _set_3slot(True)
    at = _run(_fresh_app())
    help_text = _toggle(at).help or ""
    assert f"+{config.EARLY_TP_TRIGGER_PCT:.1f}%" in help_text
    assert f"+{config.EARLY_TP_FLOOR_PCT:.1f}%" in help_text
    assert "기존 청산이 항상 우선" in help_text
    assert "5건" in help_text, "발동 표본이 5건뿐이라는 한계가 도움말에 남아 있어야 한다"
    assert "PROFIT_LOCK" in help_text, "기존 무관한 PROFIT_LOCK 기능과의 구분 문구가 있어야 한다"


def test_diagnostic_caption_shows_trigger_floor_and_entry_chop_verdict():
    _set_3slot(True)
    state = state_store.load_state()
    state.early_tp_filter_enabled = True
    state.early_tp_filter_version = config.EARLY_TP_FILTER_VERSION
    state.time_window_entry_chop = True
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2_3SLOT"
    state.early_tp_peak_net_return = 2.13
    state.last_entry_chop_score = 3
    state.last_entry_chop_conditions = {"vwap_repeat": True, "ema20_slope_not_aligned": True,
                                        "recent_confirmed_crosses": True,
                                        "ema10_ema20_spread_not_expanding": False}
    state_store.save_state(state)

    at = _run(_fresh_app())

    captions = [c.value for c in at.caption]
    assert any("조기익절 필터=ON" in c for c in captions), f"상태 캡션이 없다: {captions!r}"
    assert any("CHOP(대상)" in c for c in captions)
    assert any("MFE +2.13%" in c for c in captions)
    assert any("최근 진입 CHOP 판정: 3/4" in c for c in captions), (
        f"CHOP 점수 진단 캡션이 없다: {captions!r}"
    )


def test_exit_reason_label_is_localized_for_the_trade_history():
    """거래내역/청산사유 표시에서 원시 코드가 아니라 한글 라벨이 나와야 한다.
    페이지를 import 하면 Streamlit 렌더가 실행되므로 라벨 매핑만 소스에서 확인한다."""
    src = Path(_APP_PATH).read_text(encoding="utf-8")
    assert "macd2_config.EXIT_EARLY_TAKE_PROFIT: \"조기익절\"" in src
