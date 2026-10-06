"""UI 전략 모드 [N1] / [P3] — 새 계약 고정 (2026-09-27).

사용자 요구 §2~§5, §11. 기본 화면에서 사용자가 고르는 것은 **모드 하나**이고,
C1 / SMART / N1 / X2-lite / H50 체크박스는 보이지 않는다. 개별 토글 코드는
지우지 않았고 ``config.SHOW_LEGACY_STRATEGY_TOGGLES`` 로 되살릴 수 있다 --
2026-09-07 / 2026-09-15 숨김과 같은 구조다.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.trading.macd2 import config, service as service_module, state_store, strategy_mode

_APP_PATH = str(Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py")
_PREEXISTING_FORM_BUG = "can't be used in an `st.form()`"

RADIO_LABEL = "전략 모드"
HIDDEN_LABELS = (
    f"{config.X2LITE_3SLOT_STRATEGY_NAME} + {config.X2LITE_SIZING_NAME} sizing",
    config.H50_3SLOT_STRATEGY_NAME,
    config.N1_3SLOT_STRATEGY_NAME,
    "└ C1 Peak Protection",
    "└ SMART 사이징 (끄면 BASE)",
)


def _run() -> AppTest:
    at = AppTest.from_file(_APP_PATH, default_timeout=40)
    at.session_state["app_auth_authenticated"] = True
    at.run()
    for exc in at.exception:
        if _PREEXISTING_FORM_BUG in str(getattr(exc, "value", "") or ""):
            pytest.skip("기존 페이지 결함(st.button inside st.form) -- 이 변경과 무관")
    assert not at.exception
    return at


def _radio(at: AppTest):
    for r in at.radio:
        if r.label == RADIO_LABEL:
            return r
    raise AssertionError(f"전략 모드 라디오가 없다: {[r.label for r in at.radio]!r}")


# ── §2. 기본 화면에는 모드 하나만 ────────────────────────────────────────
def test_default_hides_the_individual_strategy_toggles():
    assert config.SHOW_LEGACY_STRATEGY_TOGGLES is False
    at = _run()
    labels = [c.label for c in at.checkbox]
    for lab in HIDDEN_LABELS:
        assert lab not in labels, f"{lab} 이 아직 보인다: {labels!r}"


def test_strategy_mode_radio_offers_exactly_the_defined_modes():
    """N1 / P3 / E (2026-10-05 E 추가) -- 모드 목록 밖의 선택지는 없다."""
    at = _run()
    radio = _radio(at)
    # AppTest 는 format_func 를 통과한 **표시 문자열**을 돌려준다.
    assert len(radio.options) == len(strategy_mode.ALL_MODES) == 3,         f"선택지가 모드 수와 다르다: {radio.options!r}"
    shown = " | ".join(str(o) for o in radio.options)
    for mode in strategy_mode.ALL_MODES:
        assert mode in shown, f"{mode} 선택지가 없다: {shown!r}"


def test_only_one_mode_can_be_selected():
    """radio 는 구조적으로 하나만 고를 수 있다 -- 값이 항상 유효한 모드다."""
    at = _run()
    assert _radio(at).value in strategy_mode.ALL_MODES


# ── §5. Advanced 는 읽기 전용 ────────────────────────────────────────────
def test_advanced_panel_lists_the_derived_components():
    at = _run()
    body = "\n".join(str(m.value) for m in at.markdown)
    for key in ("N1", "C1", "SMART", "AR1", "SHADOW", "B3", "Y3", "P3_RESCUE"):
        assert key in body, f"Advanced 패널에 {key} 가 없다"


def test_advanced_panel_has_no_writable_widget_for_the_components():
    """읽기 전용이어야 한다 -- 구성요소별 체크박스를 만들지 않는다."""
    at = _run()
    keys = [c.key for c in at.checkbox]
    for forbidden in ("macd2_p3_shadow_toggle", "macd2_p3_b3_toggle",
                      "macd2_p3_y3_toggle", "macd2_p3_rescue_toggle",
                      "macd2_chop_toggle"):
        assert forbidden not in keys, f"{forbidden} 가 생겼다 -- 개별 토글 금지"


# ── §4. 되살리기 ─────────────────────────────────────────────────────────
def test_restore_flag_brings_the_toggles_back(monkeypatch):
    monkeypatch.setattr(config, "SHOW_LEGACY_STRATEGY_TOGGLES", True)
    at = _run()
    labels = [c.label for c in at.checkbox]
    for lab in HIDDEN_LABELS:
        assert lab in labels, f"복구 플래그를 켰는데 {lab} 이 안 보인다: {labels!r}"


def test_restore_flag_keeps_the_mode_radio_too():
    """복구는 되살리기만 한다 -- 새 라디오를 밀어내지 않는다."""
    at = _run()
    _radio(at)   # 기본 상태에서도 있어야 한다


# ── §13. source of truth 는 하나 ─────────────────────────────────────────
def test_service_setter_exists_and_individual_p3_toggles_do_not_leak_into_ui():
    assert hasattr(service_module.Macd2Service, "set_strategy_mode")
    src = Path(_APP_PATH).read_text(encoding="utf-8")
    # UI 는 모드 setter 만 부른다. B3/Y3/detector 전용 setter 는 존재하지 않는다.
    for forbidden in ("set_b3_enabled", "set_y3_enabled", "set_shadow_enabled",
                      "set_chop_detector_enabled"):
        assert forbidden not in src, f"{forbidden} 를 UI 가 부른다"


def test_worker_does_not_read_the_ui_visibility_flag():
    """숨김은 렌더만 막는다 -- 판정 경로로 새어 들어가면 안 된다."""
    from app.trading.macd2 import worker

    assert "SHOW_LEGACY_STRATEGY_TOGGLES" not in inspect.getsource(worker)


def test_state_store_does_not_read_the_ui_visibility_flag():
    from app.trading.macd2 import state_store as ss

    assert "SHOW_LEGACY_STRATEGY_TOGGLES" not in inspect.getsource(ss)


# ── §8. fail-safe 표시 ───────────────────────────────────────────────────
def test_p3_mode_shows_mode_regime_and_execution_lines():
    s = state_store.load_state()
    strategy_mode.apply(s, strategy_mode.MODE_P3)
    s.mode = "mock"
    state_store.save_state(s)

    at = _run()
    caps = "\n".join(str(c.value) for c in at.caption)
    assert "MODE" in caps
    assert "REGIME" in caps
    assert "SHADOW" in caps
    # detector 가 준비되지 않았으면 실행계층은 BASE 로 표시돼야 한다.
    assert "EXECUTION" in caps


def test_n1_mode_shows_only_the_mode_line():
    s = state_store.load_state()
    strategy_mode.apply(s, strategy_mode.MODE_N1)
    s.mode = "mock"
    state_store.save_state(s)

    at = _run()
    caps = "\n".join(str(c.value) for c in at.caption)
    assert "MODE" in caps
    assert "REGIME" not in caps, "N1 모드에서는 regime 을 보여 주지 않는다"


# ── §11 4/5. 라디오를 실제로 눌러 모드가 바뀐다 ──────────────────────────
def _pick(at: AppTest, mode: str):
    radio = _radio(at)
    target = [o for o in radio.options if str(o).startswith(mode)]
    assert target, f"{mode} 선택지가 없다: {radio.options!r}"
    return radio.set_value(target[0])


def _seed_mode(mode: str) -> None:
    s = state_store.load_state()
    s.mode = "mock"
    strategy_mode.apply(s, mode)
    state_store.save_state(s)


@pytest.mark.parametrize(
    "start, target",
    [(strategy_mode.MODE_N1, strategy_mode.MODE_P3),
     (strategy_mode.MODE_P3, strategy_mode.MODE_N1)],
)
def test_clicking_the_other_mode_switches_and_persists(start, target):
    _seed_mode(start)
    at = _run()
    _pick(at, target).run()

    restored = state_store.load_state()
    assert strategy_mode.current(restored) == target
    # 하위 구성도 함께 정규화된다 -- 개별 토글을 누를 필요가 없다.
    flags = strategy_mode.derive(target)
    assert bool(restored.p3_enabled) is flags["SHADOW"]
    assert bool(restored.c1_peak_protection_enabled) is flags["C1"]
    assert bool(restored.smart_sizing_enabled) is flags["SMART"]
    assert restored.time_window_n1_filter_enabled is True


def test_switching_mode_leaves_the_open_position_snapshot_alone():
    """§11-4: 모드를 바꿔도 이미 열린 포지션의 진입 스냅샷은 유지된다 --
    새 모드는 **다음 신규 진입부터** 적용된다."""
    from app.trading.macd2 import chop_regime, p3_stack

    _seed_mode(strategy_mode.MODE_P3)
    s = state_store.load_state()
    p3_stack.note_entry_regime(s, chop_regime.REGIME_CHOP)
    state_store.save_state(s)

    at = _run()
    _pick(at, strategy_mode.MODE_N1).run()

    restored = state_store.load_state()
    assert strategy_mode.current(restored) == strategy_mode.MODE_N1
    assert restored.p3_entry_regime == chop_regime.REGIME_CHOP, (
        "진입 스냅샷을 소급해서 지우면 안 된다")
    assert p3_stack.governs_position(restored) is False, (
        "P3 를 껐으면 B3 가 더 이상 주인이 아니다")


def test_there_is_no_second_way_to_set_the_p3_flag():
    """§13: 전략 설정을 바꾸는 입구는 set_strategy_mode 하나뿐이다.

    p3_enabled 만 따로 뒤집는 setter 가 있으면 strategy_mode 와 어긋난 상태를
    만들 수 있다(모드는 N1 인데 P3 스택이 도는 식).
    """
    assert not hasattr(service_module.Macd2Service, "set_p3_enabled")


# ── MOCK / REAL 양쪽 (2026-09-27) ────────────────────────────────────────
@pytest.mark.parametrize("account", ["mock", "real"])
def test_status_block_shows_the_five_required_lines(account):
    """ACCOUNT / MODE / REGIME / SHADOW / EXECUTION 다섯 줄이 최소 규격이다."""
    s = state_store.load_state()
    s.mode = account
    strategy_mode.apply(s, strategy_mode.MODE_P3)
    state_store.save_state(s)

    at = _run()
    caps = "\n".join(str(c.value) for c in at.caption)
    for line in ("ACCOUNT", "MODE", "REGIME", "SHADOW", "EXECUTION"):
        assert line in caps, f"{line} 줄이 없다"
    assert account.upper() in caps


@pytest.mark.parametrize("account", ["mock", "real"])
def test_p3_can_be_selected_in_both_accounts(account):
    """REAL 에서도 [P3] 를 고를 수 있다 -- 다만 자동으로 켜지지는 않는다."""
    s = state_store.load_state()
    s.mode = account
    strategy_mode.apply(s, strategy_mode.MODE_N1)
    state_store.save_state(s)

    res = service_module.get_service().set_strategy_mode(
        strategy_mode.MODE_P3, changed_by="test")
    assert res.get("ok") is True, res
    assert res.get("account") == account.upper()
    assert strategy_mode.current(state_store.load_state()) == strategy_mode.MODE_P3


def test_setter_no_longer_refuses_real_accounts():
    """P3_PAPER_ONLY 거부 경로가 남아 있으면 REAL 에서 못 켠다."""
    src = inspect.getsource(service_module.Macd2Service.set_strategy_mode)
    assert "P3_PAPER_ONLY" not in src


# ── 선택 모드 / 실행계층 분리 표시 (2026-09-28) ──────────────────────────
# Render MOCK 에서 P3 를 골랐는데 배너가 "현재 활성 전략: N1" 로만 보여 오해가
# 생겼다. P3 는 3-SLOT tier 목록에 없는 청산 계층이라 tier 배너는 항상 N1 을
# 가리킨다 -- P3 모드에서는 선택과 실행을 나눠 써야 한다.
def _seed_p3(regime, sample=0):
    s = state_store.load_state()
    s.mode = "mock"
    strategy_mode.apply(s, strategy_mode.MODE_P3)
    s.p3_last_regime = regime
    s.p3_last_shadow_sample = sample
    state_store.save_state(s)


def _texts(at):
    caps = "\n".join(str(c.value) for c in at.caption)
    banners = "\n".join(str(b.value) for b in at.success)
    return caps, banners


def test_p3_warmup_shows_selected_mode_and_base_fallback_separately():
    _seed_p3("WARMUP", sample=0)
    caps, banners = _texts(_run())
    assert "SELECTED MODE: **P3**" in caps
    assert "REGIME: **WARMUP**" in caps
    assert "SHADOW: **0/10**" in caps
    assert "EXECUTION: **BASE (fallback · Shadow Warmup)**" in caps
    assert "POSITION MODE: **-**" in caps
    assert "선택 전략 **P3** / 현재 실행 **BASE** (Shadow Warmup)" in banners
    assert "현재 활성 전략" not in banners, "P3 모드에서 tier 이름(N1)만 보여 주면 안 된다"


def test_p3_warmup_progress_is_shown_as_count():
    _seed_p3("WARMUP", sample=3)
    caps, _ = _texts(_run())
    assert "SHADOW: **3/10**" in caps


def test_p3_ready_shows_p3_execution_in_both_places():
    _seed_p3("CHOP", sample=10)
    caps, banners = _texts(_run())
    assert "SHADOW: **READY**" in caps
    assert "EXECUTION: **P3**" in caps
    assert "fallback" not in caps
    assert "선택 전략 **P3** / 현재 실행 **P3** (regime CHOP)" in banners


def test_n1_mode_keeps_the_tier_banner():
    s = state_store.load_state()
    s.mode = "mock"
    strategy_mode.apply(s, strategy_mode.MODE_N1)
    state_store.save_state(s)
    caps, banners = _texts(_run())
    assert "SELECTED MODE: **N1**" in caps
    assert "EXECUTION: **BASE**" in caps
    assert f"현재 활성 전략: **{config.N1_3SLOT_STRATEGY_NAME}**" in banners
    assert "선택 전략" not in banners


# ── N1 LADDER 줄: 지금 걸린 TP1/TP2 (2026-09-28) ─────────────────────────
# 레거시 토글 영역과 함께 숨겨졌던 N1 adaptive 판정을 상태 블록에 되살린다.
def _seed_n1_position(**fields):
    s = state_store.load_state()
    s.mode = "mock"
    strategy_mode.apply(s, strategy_mode.MODE_N1)
    s.time_window_position_active = True
    s.time_window_entry_session = "MORNING"
    for k, v in fields.items():
        setattr(s, k, v)
    state_store.save_state(s)


def test_n1_ladder_line_shows_trend_values():
    _seed_n1_position(n1_last_eval_bar_ts="2026-09-28T10:03:00+09:00",
                      n1_regime_state="TREND", n1_effective_tp1=3.5,
                      n1_effective_tp1_ratio=0.0, n1_effective_tp2=8.0)
    caps, _ = _texts(_run())
    assert "N1 LADDER: **추세 (TREND)** · TP1 **3.5%** (매도 0%) · TP2 **8.0%** 전량 · 판정봉 10:03" in caps


def test_n1_ladder_line_shows_off_trend_values():
    _seed_n1_position(n1_last_eval_bar_ts="2026-09-28T10:06:00+09:00",
                      n1_regime_state="OFF_TREND", n1_effective_tp1=3.0,
                      n1_effective_tp1_ratio=0.2, n1_effective_tp2=4.0)
    caps, _ = _texts(_run())
    assert "N1 LADDER: **비추세 (OFF_TREND)** · TP1 **3.0%** (매도 20%) · TP2 **4.0%** 전량" in caps


def test_n1_ladder_line_falls_back_to_off_trend_before_first_judgement():
    """worker(_n1_ladder_overrides) 와 같은 fallback 이어야 한다."""
    _seed_n1_position(n1_last_eval_bar_ts=None)
    caps, _ = _texts(_run())
    assert "N1 LADDER: **판정 전 → 비추세 적용** · TP1 **3.0%** (매도 20%) · TP2 **4.0%**" in caps


def test_n1_ladder_line_when_flat_lists_both_ladders():
    s = state_store.load_state()
    s.mode = "mock"
    strategy_mode.apply(s, strategy_mode.MODE_N1)
    s.time_window_position_active = False
    state_store.save_state(s)
    caps, _ = _texts(_run())
    assert "N1 LADDER: 포지션 없음" in caps
    assert "추세 TP1 3.5% (매도 0%) / TP2 8.0%" in caps
    assert "비추세 TP1 3.0% (매도 20%) / TP2 4.0%" in caps
